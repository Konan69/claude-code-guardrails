#!/usr/bin/env python3
"""Admit explicit commits, non-production SSH targets, and non-credential reads.

This is a guard for ordinary shell commands, not a shell interpreter or a sandbox.
It never executes the submitted command. SSH resolution uses configuration only.
"""

from collections import namedtuple
from itertools import chain
import fnmatch
import json
import os
import re
import shlex
import sys
import time


from policy import CONFIG, path
POLICY = CONFIG["guarding"]
AGENCY = path(POLICY["shared_checkout"])
STORES = POLICY["credential_stores"]
PROTECTED = {path("~/" + name) for name in STORES}
READERS = {"cat", "less", "head", "tail", "more", "sed", "awk", "grep", "rg",
           "jq", "base64", "strings", "dd", "cp", "install"}
CREDENTIAL_REASON = POLICY["credential_reason"]
COMMIT_REASON = POLICY["commit_reason"]
HOST_REASON = POLICY["host_reason"]


# Invocations retain directory identity without dataclasses' inspect/ast startup cost.
Command = namedtuple("Command", ["cwd", "argv", "redirections"], defaults=[()])
Redirection = namedtuple("Redirection", ["operator", "target", "body"], defaults=[None])
METADATA = {"ls", "stat", "test", "[", "du", "file", "readlink", "realpath"}
SHELLS = {"bash", "sh", "zsh"}
INTERPRETERS = {"node", "bun", "deno", "ruby", "perl", "jq"}
SHELL_WORD = r'''(?:[^\s;&|()<>'"\\]|\\[\s\S]|'[^']*'|"(?:\\[\s\S]|[^"\\])*")+'''
SHELL_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>\#.*)|"
    r"(?P<redirect>(?:[0-9]+|\{[A-Za-z_][A-Za-z_0-9]*\})?(?:&>>|&>|<<<|<<-|<<|>>|<>|>&|<&|>\||[<>]))|"
    r"(?P<word>" + SHELL_WORD + r")|(?P<separator>&&|\|\||[;&|()])"
)
SHELL_TARGET = re.compile(r"\s*(" + SHELL_WORD + r")")
OPERATIONS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def without_heredocs(source, documents=None):
    """Detach heredoc bodies and retain them by delimiter for the receiving command.

    shlex recognizes quoted delimiters and keeps quoted <<WORD text together.
    Multiple documents consume bodies in declaration order; <<- strips tabs only.
    A command-substitution opener can leave the outer quote open on this line.
    Recover its literal delimiter so the complete command can be lexed after body removal.
    """
    if documents is None:
        documents = {}
    pending = []
    kept = []
    body = []
    for line in source.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                documents.setdefault(delimiter, []).append("".join(body))
                body = []
                pending.pop(0)
            else:
                body.append(line.lstrip("\t") if strip_tabs else line)
            continue
        kept.append(line)
        if "<<" not in line:
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            delimiters = list(re.finditer(r"<<(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2", line))
            pending.extend((match.group(3), bool(match.group(1))) for match in delimiters)
            continue
        for index, token in enumerate(tokens[:-1]):
            if token == "<<":
                delimiter = tokens[index + 1]
                strip_tabs = delimiter.startswith("-")
                pending.append((delimiter[1:] if strip_tabs else delimiter, strip_tabs))
    return "".join(kept)


def path_at(value, cwd):
    """Resolve spelling and symlinks without opening any file's contents."""
    path = os.path.expandvars(os.path.expanduser(value))
    return os.path.realpath(os.path.join(cwd, path))


def credential(value, cwd):
    """Store names are centralized so Read, redirection, and reader commands agree."""
    return path_at(value, cwd) in PROTECTED


def credential_pattern(value, cwd):
    """Match shell globs without filesystem expansion or wildcard access to hidden components."""
    pattern = path_at(value, cwd).split(os.sep)
    for protected in PROTECTED:
        pieces = protected.split(os.sep)
        if len(pieces) == len(pattern) and all(
            (not piece.startswith(".") or part.startswith(".")) and fnmatch.fnmatchcase(piece, part)
            for piece, part in zip(pieces, pattern)
        ):
            return True
    return False


def credential_text(source, cwd):
    """Check path spellings, not reader names, without expanding globs against real files.

    The caller removes heredoc data first. This check also runs on malformed shell
    text, and catches paths embedded in Python strings or quoted substitutions.
    """
    home = os.path.expanduser("~")
    # Node's direct HOME concatenation is a common literal path, not general JS evaluation.
    expanded = re.sub(r"process\.env\.HOME\s*\+\s*(['\"])(?=/)",
                      lambda match: match.group(1) + home, source)
    expanded = expanded.replace("${HOME}", home).replace("$HOME", home)
    expanded = re.sub(r"(?<![\w/])~(?=/)", lambda _: home, expanded)
    for match in re.finditer(r"(?:/|\.{1,2}/)[^\s'\"`;|&<>(){}]+", expanded):
        if credential_pattern(match.group(), cwd):
            return CREDENTIAL_REASON
    return None


def substitution_end(source, start, closing):
    """Find a common substitution's end while respecting quotes and nested parentheses.

    This only finds structure; the body goes back through commands rather than
    being evaluated. A backtick body has its own simple escaped closing delimiter.
    """
    index, level, quote = start, 1, None
    while index < len(source):
        char = source[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if closing == "`":
            if char == "`":
                return index
        elif quote == "'":
            if char == "'":
                quote = None
        elif source.startswith("$(", index) or (quote is None and source[index:index + 2] in {"<(", ">("}):
            index = substitution_end(source, index + 2, ")")
        elif char == "`":
            index = substitution_end(source, index + 1, "`")
        elif char == '"':
            quote = None if quote == '"' else '"'
        elif quote is None:
            if char == "'":
                quote = char
            elif char == "(":
                level += 1
            elif char == ")":
                level -= 1
                if level == 0:
                    return index
        index += 1
    raise ValueError("Unfinished shell substitution")


def substitutions(source):
    """Mask executable substitutions as words, retaining their bodies and token positions.

    Single quotes are data. Double quotes still execute dollar and backtick bodies.
    Keeping each body's token position preserves the directory at its invocation.
    """
    kept, bodies = [], []
    index, quote, length = 0, None, 0
    while index < len(source):
        char = source[index]
        width = 0
        if char == "#" and quote is None and (index == 0 or source[index - 1] in " \t\n;&|()"):
            end = source.find("\n", index)
            end = len(source) if end == -1 else end
            piece = source[index:end]
            kept.append(piece)
            length += len(piece)
            index = end
            continue
        if char == "\\" and quote != "'":
            piece = source[index:index + 2]
            kept.append(piece)
            length += len(piece)
            index += 2
            continue
        if quote != "'":
            if source.startswith("$(", index) or (quote is None and source[index:index + 2] in {"<(", ">("}):
                width, closing = 2, ")"
            elif char == "`":
                width, closing = 1, "`"
        if width:
            end = substitution_end(source, index + width, closing)
            bodies.append((length, source[index + width:end]))
            piece = "__substitution__"
            kept.append(piece)
            length += len(piece)
            index = end + 1
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        kept.append(char)
        length += 1
        index += 1
    return "".join(kept), bodies


def in_agency(path):
    """Use directory boundaries so agency-other cannot inherit the shared-checkout policy."""
    return os.path.commonpath([str(path), str(AGENCY)]) == str(AGENCY)


def shell_tokens(source):
    """Consume redirects with their targets before punctuation can split descriptor duplication.

    Raw words preserve quoting until shlex decodes them. A descriptor belongs to a
    redirect only when adjacent, so a numeric positional path remains an argument.
    """
    source, bodies = substitutions(source)
    offset, body_index = 0, 0
    while offset < len(source):
        match = SHELL_TOKEN.match(source, offset)
        if match is None:
            raise ValueError("Unsupported shell token")
        offset = match.end()
        kind = match.lastgroup
        if kind == "space":
            if "\n" in match.group():
                yield "separator", ";"
            continue
        if kind == "comment":
            continue
        value = match.group()
        if kind == "redirect":
            target = SHELL_TARGET.match(source, offset)
            if target is None:
                raise ValueError("Missing redirection target")
            offset = target.end()
            operator = re.sub(r"^(?:[0-9]+|\{[A-Za-z_][A-Za-z_0-9]*\})", "", value)
            value = Redirection(operator, shlex.split(target.group(1))[0])
        elif kind == "word":
            value = shlex.split(value)[0]
        while body_index < len(bodies) and bodies[body_index][0] < offset:
            yield "substitution", bodies[body_index][1]
            body_index += 1
        yield kind, value


def commands(source, cwd, depth=0, documents=None):
    """Keep command arguments and redirections separate without evaluating substitutions."""
    if depth > 3:
        return
    if documents is None:
        documents = {}
        source = without_heredocs(source, documents)
    tokens = shell_tokens(source)
    parts, redirections, directories = [], [], []
    for kind, token in chain(tokens, [("separator", ";")]):
        if kind == "substitution":
            yield from commands(token, cwd, depth + 1, documents)
            continue
        if kind == "redirect":
            if token.operator in {"<<", "<<-"}:
                bodies = documents.get(token.target, [])
                token = token._replace(body=bodies.pop(0) if bodies else "")
            redirections.append(token)
            continue
        if kind == "word":
            parts.append(token)
            continue
        if token == "(" and not parts and not redirections:
            directories.append(cwd)
            continue
        if token == ")" and not parts and not redirections:
            if directories:
                cwd = directories.pop()
            continue
        if not parts and not redirections:
            continue
        argv = unwrap(parts)
        yield Command(cwd, tuple(argv), tuple(redirections))
        command_documents = [redirect for redirect in redirections if redirect.body is not None]
        parts, redirections = [], []
        name = os.path.basename(argv[0]) if argv else ""
        if name == "cd" and len(argv) > 1:
            cwd = path_at(argv[-1], cwd)
        elif name in SHELLS:
            offset = next((index + 1 for index, arg in enumerate(argv[1:], 1)
                           if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg)), len(argv))
            if offset < len(argv):
                yield from commands(argv[offset], cwd, depth + 1)
        if name in SHELLS:
            for redirect in command_documents:
                yield from commands(redirect.body, cwd, depth + 1)
        if token == ")" and directories:
            cwd = directories.pop()


def unwrap(parts):
    """Common launchers must not hide the executable from a command policy."""
    argv = list(parts)
    while argv:
        name = os.path.basename(argv[0])
        if argv[0] in {"{", "!", "if", "then", "elif", "else", "do", "while", "until", "("}:
            argv.pop(0)
        elif re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", argv[0]):
            argv.pop(0)
        elif name in LAUNCHERS:
            argv.pop(0)
            while argv and argv[0].startswith("-"):
                option = argv.pop(0)
                if option == "--":
                    break
                valued = LAUNCHERS[name]
                if option in valued and argv:
                    argv.pop(0)
                elif not option.startswith("--"):
                    # A value flag consumes its suffix, or the next word at bundle end.
                    for position, flag in enumerate(option[1:], 1):
                        if "-" + flag in valued:
                            if position == len(option) - 1 and argv:
                                argv.pop(0)
                            break
            if name in {"timeout", "flock", "taskset"} and argv:
                argv.pop(0)
            if name == "flock" and argv and argv[0] in {"-c", "--command"}:
                argv = shlex.split(argv[1])
        else:
            break
    return argv


# Only launcher flags that consume a separate argument belong here.
LAUNCHERS = {
    "command": set(), "exec": {"-a"}, "nohup": set(),
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-h", "-p"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "time": {"-o", "--output", "-f", "--format"},
    "nice": {"-n", "--adjustment"},
    "flock": {"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
    "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"},
    "taskset": set(), "chronic": set(),
    "watch": {"-n", "--interval"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L", "-n", "-P", "-s",
              "--max-lines", "--max-args", "--max-procs", "--max-chars"},
}


def git_invocation(command):
    """Git's global directory flags override the shell directory before the subcommand."""
    argv = list(command.argv[1:])
    cwd = command.cwd
    while argv and argv[0].startswith("-"):
        option = argv.pop(0)
        if option in {"-C", "--work-tree"}:
            cwd = path_at(argv.pop(0), cwd)
        elif option.startswith("--work-tree="):
            cwd = path_at(option.partition("=")[2], cwd)
        elif option.startswith("-C") and option != "-C":
            cwd = path_at(option[2:], cwd)
        elif option == "--git-dir":
            directory = path_at(argv.pop(0), cwd)
            if in_agency(directory):
                cwd = directory
        elif option.startswith("--git-dir="):
            directory = path_at(option.partition("=")[2], cwd)
            if in_agency(directory):
                cwd = directory
        elif option in {"-c", "--namespace", "--config-env"}:
            argv.pop(0)
    return cwd, argv


def operation_active(command, cwd, argv):
    """Stat per-worktree operation markers without spawning Git under scheduler load.

    Git refuses partial merge commits. A .git file points to the worktree's own
    operation directory, not the common directory that owns objects and refs.
    No staged-path subprocess is needed because ordinary commits always name paths.
    """
    prefix = command.argv[:len(command.argv) - len(argv)]
    directory = None
    base = command.cwd
    for index, option in enumerate(prefix):
        if option == "-C":
            base = path_at(prefix[index + 1], base)
        elif option.startswith("-C") and len(option) > 2:
            base = path_at(option[2:], base)
        elif option == "--git-dir":
            directory = path_at(prefix[index + 1], base)
        elif option.startswith("--git-dir="):
            directory = path_at(option.partition("=")[2], base)
    if directory is None:
        current = cwd
        while True:
            candidate = os.path.join(current, ".git")
            if os.path.isfile(candidate):
                with open(candidate) as pointer:
                    line = pointer.readline(4096).strip()
                if line.startswith("gitdir: "):
                    directory = path_at(line[8:], current)
                break
            if os.path.isdir(candidate):
                directory = candidate
                break
            if os.path.isfile(os.path.join(current, "HEAD")) and os.path.isdir(os.path.join(current, "objects")):
                directory = current
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return directory is not None and any(os.path.exists(os.path.join(directory, marker)) for marker in OPERATIONS)


def commit_reason(command):
    """Require named paths except when Git owns a resumable operation's whole staged result."""
    cwd, argv = git_invocation(command)
    if not in_agency(cwd) or not argv or argv[0] != "commit":
        return None
    index = 1
    valued = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c",
              "--reedit-message", "--author", "--date", "-t", "--template", "--trailer",
              "--fixup", "--squash", "--cleanup", "--pathspec-from-file"}
    paths = []
    while index < len(argv):
        option = argv[index]
        if option == "--":
            paths.extend(argv[index + 1:])
            break
        if option == "--all":
            return COMMIT_REASON
        if option.startswith("-") and not option.startswith("--"):
            for position, flag in enumerate(option[1:], 1):
                if flag == "a":
                    return COMMIT_REASON
                if flag in "mFCct":
                    if position == len(option) - 1:
                        index += 1
                    break
        if not option.startswith("-"):
            paths.append(option)
        index += 2 if option.startswith("--") and option in valued else 1
    root = cwd
    while root != os.path.dirname(root) and not os.path.exists(os.path.join(root, ".git")):
        root = os.path.dirname(root)
    if any(path in {".", "./", ":/", ":", "*"} or path_at(path, cwd) == root for path in paths):
        return COMMIT_REASON
    return None if paths or operation_active(command, cwd, argv) else COMMIT_REASON


def reader_arguments(argv):
    """Search patterns are data; explicit pattern files and remaining operands can read contents."""
    name = os.path.basename(argv[0])
    if name not in {"rg", "grep"}:
        return argv[1:]
    paths = []
    pattern_seen = False
    valued = {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-g", "--glob",
              "--iglob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C",
              "--after-context", "--before-context", "--context", "--encoding"}
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--":
            paths.extend(argv[index + (1 if pattern_seen else 2):])
            break
        option, equals, attached = value.partition("=")
        if value.startswith("-") and not value.startswith("--"):
            for position, flag in enumerate(value[1:], 1):
                if "-" + flag in valued:
                    option = "-" + flag
                    attached = value[position + 1:]
                    equals = bool(attached)
                    break
        if option in valued:
            argument = attached if equals else argv[index + 1]
            index += 0 if equals else 1
            if option in {"-e", "--regexp", "-f", "--file"}:
                pattern_seen = True
            if option in {"-f", "--file"}:
                paths.append(argument)
        elif not value.startswith("-"):
            if pattern_seen:
                paths.append(value)
            pattern_seen = True
        index += 1
    return paths


def read_reason(command):
    """Content readers, executable interpreter text and redirects share the protected-store list."""
    argv = command.argv
    name = os.path.basename(argv[0]) if argv else ""
    for redirection in command.redirections:
        if redirection.operator in {"<", "<>"} and credential_pattern(redirection.target, command.cwd):
            return CREDENTIAL_REASON
        if redirection.body is not None and (name in INTERPRETERS or re.fullmatch(r"python[\d.]*", name)):
            denied = credential_text(redirection.body, command.cwd)
            if denied:
                return denied
    if name in METADATA or name in SHELLS:
        return None
    arguments = reader_arguments(argv) if name in READERS else list(argv[1:])
    if name == "git":
        # A commit message is data, including bundled -qm and --message= spellings.
        arguments = []
        index = 1
        while index < len(argv):
            argument = argv[index]
            if argument in {"-m", "--message"} or re.fullmatch(r"-[^-]*m", argument):
                index += 2
                continue
            if argument.startswith("--message=") or re.match(r"-[^-]*m.+", argument):
                index += 1
                continue
            arguments.append(argument)
            index += 1
    for argument in arguments:
        value = argument.partition("=")[2] if argument.startswith("if=") else argument
        if credential_pattern(value, command.cwd) or credential_text(value, command.cwd):
            return CREDENTIAL_REASON
    return None


def ssh_arguments(argv, transfer=False):
    """Forward resolution options; -G before the host inspects config without connecting."""
    options, operands = [], []
    index = 1
    valued = {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L",
              "-l", "-m", "-O", "-o", "-p", "-P", "-Q", "-R", "-S", "-W", "-w"}
    if transfer:
        valued = {"-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-X"}
    forwarded = {"-F", "-o", "-J", "-l", "-p", "-i"}
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            operands.extend(argv[index + 1:])
            break
        if not arg.startswith("-"):
            operands.append(arg)
            if not transfer:
                break
        elif arg.startswith("-") and not transfer:
            # getopt stops at the first value-taking flag; its suffix is data, even G.
            for position, flag in enumerate(arg[1:], 1):
                option = "-" + flag
                if option in valued:
                    value = arg[position + 1:]
                    if not value:
                        index += 1
                        value = argv[index]
                    if option in forwarded:
                        options.extend([option, value])
                    break
                if flag == "G":
                    return options, []
        elif arg in valued:
            value = argv[index + 1]
            if arg in forwarded:
                options.extend([arg, value])
            index += 1
        elif arg[:2] in valued and len(arg) > 2:
            if arg[:2] in forwarded:
                options.extend([arg[:2], arg[2:]])
        index += 1
    return options, operands


def remote_host(operand):
    """Transfer endpoints may use user@host:path, bracketed IPv6, or a URL."""
    if "://" in operand:
        from urllib.parse import urlsplit
        return urlsplit(operand).hostname
    match = re.match(r"^(?:[^/@:]+@)?(\[[^]]+\]|[^/:]+):", operand)
    return match.group(1).strip("[]") if match else None


def destinations(command):
    """Extract transfer endpoints; rsync's literal SSH options retain custom config files."""
    argv = command.argv
    name = os.path.basename(argv[0])
    if name == "ssh":
        options, hosts = ssh_arguments(argv)
        return [(options, host) for host in hosts]
    if name in {"scp", "sftp"}:
        options, operands = ssh_arguments(argv, transfer=True)
    else:
        options, operands = [], []
        index = 1
        while index < len(argv):
            argument = argv[index]
            if argument in {"-e", "--rsh"}:
                options, _ = ssh_arguments(shlex.split(argv[index + 1]))
                index += 1
            elif argument.startswith("--rsh="):
                options, _ = ssh_arguments(shlex.split(argument.partition("=")[2]))
            elif not argument.startswith("-"):
                operands.append(argument)
            index += 1
    if name == "sftp":
        return [(options, remote_host(operand) or operand) for operand in operands]
    return [(options, host) for operand in operands if (host := remote_host(operand))]


def host_reason(command, deadline, resolutions):
    """Mirror refusedHost while caching each (options, host) within this hook invocation.

    Each ssh -G gets up to 0.4 s inside the shared 1.2 s cap. Resolution never
    connects; uncertainty asks for a retry without claiming a production target.
    """
    import subprocess
    for options, host in destinations(command):
        if host.rsplit("@", 1)[-1].split(":", 1)[0].lower() in POLICY["protected_hosts"]:
            return HOST_REASON
        key = (tuple(options), host)
        if key not in resolutions:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"Could not resolve {host} in time; retry."
            try:
                result = subprocess.run(
                    ["ssh", "-G", *options, "--", host], text=True, capture_output=True,
                    timeout=min(0.4, remaining), check=True,
                )
            except subprocess.TimeoutExpired:
                return f"Could not resolve {host} in time; retry."
            resolutions[key] = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
        fields = resolutions[key]
        if fields.get("hostname", "").strip() in POLICY["protected_hosts"]:
            return HOST_REASON
        if re.search(POLICY["protected_proxy_pattern"], fields.get("proxycommand", "")):
            return HOST_REASON
    return None


def reason(data):
    """Tool input is untrusted; all policy work stays inside a bounded fail-open entry point."""
    cwd = os.path.realpath(data["cwd"])
    tool = data["tool_name"]
    entry = data["tool_input"]
    if tool == "Read":
        return CREDENTIAL_REASON if credential(entry["file_path"], cwd) else None
    if tool != "Bash":
        return None
    source = without_heredocs(entry["command"])
    resolutions = {}
    deadline = time.monotonic() + 1.2
    invocations = commands(entry["command"], cwd)
    while True:
        try:
            command = next(invocations)
        except StopIteration:
            break
        except ValueError:
            denied = credential_text(source, cwd)
            if denied is not None:
                return denied
            # Even unsupported shell syntax cannot hide a literal production name.
            if any(re.search(r"\b" + re.escape(host) + r"\b", source) for host in POLICY["protected_hosts"]):
                return HOST_REASON
            raise
        denied = read_reason(command)
        name = os.path.basename(command.argv[0]) if command.argv else ""
        if denied is None and name == "git":
            denied = commit_reason(command)
        if denied is None and name in {"ssh", "scp", "rsync", "sftp"} and destinations(command):
            denied = host_reason(command, deadline, resolutions)
        if denied is not None:
            return denied
    return None


def main():
    """Crashes allow normal permission processing and reveal only the error's class."""
    try:
        denied = reason(json.load(sys.stdin))
        if denied is not None:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": denied,
            }}))
    except Exception as error:
        print(f"u17 guarding failed open ({type(error).__name__})", file=sys.stderr)


if __name__ == "__main__":
    main()
