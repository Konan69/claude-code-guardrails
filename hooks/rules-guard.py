#!/usr/bin/env python3
"""PreToolUse guards for GitHub merges, branch pushes, delegates and new threads.

Shell checks are textual guards, not a sandbox. Gated actions fail closed.
"""
import json, os, re, shlex, subprocess, sys, time

from policy import CONFIG, path
POLICY = CONFIG["rules"]
ALLOWED_MODELS = set(POLICY["allowed_models"])
BAD_EFFORT = set(POLICY["bad_effort"])
CODEX_EFFORTS = set(POLICY["codex_efforts"])
THREAD_TOOLS = set(POLICY["thread_tools"])
MARKER = path(POLICY["thread_marker"])


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


CANON_FLAGS = {"-s", "--squash", "-m", "--merge", "-r", "--rebase", "-d", "--delete-branch"}
OPERATORS = re.compile(r"[;&|()<>`$\n]|\bthen\b|\bdo\b")


PROSE_ONLY = re.compile(r"(^|/)[^/]+\.(md|mdx|txt)$|^docs/")


def prose_only_head(repo, pr):
    """Head sha when every changed file is prose.

    A docs or instruction change has no runtime to review, so it skips the cross-family verdict.
    The merge is still bound to the head that was checked."""
    try:
        r = subprocess.run(["gh", "pr", "view", pr, "-R", repo, "--json", "files,headRefOid"],
                           capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
    except Exception:
        return None
    files = [f["path"] for f in data.get("files", [])]
    if files and all(PROSE_ONLY.search(f) for f in files):
        return data["headRefOid"]
    return None


def current_head(repo, pr):
    """The PR's head sha on GitHub, or None when it cannot be read."""
    try:
        r = subprocess.run(["gh", "pr", "view", pr, "-R", repo, "--json", "headRefOid"],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)["headRefOid"]
    except Exception:
        return None


def gate(repo, pr, match):
    """Bind the merge to the current head. The verdict ledger is optional."""
    head = current_head(repo, pr)
    if head is None:
        deny("Could not read the PR's head; not merging unbound.")
    if match != head:
        deny(f"Bind the merge to the current head: add --match-head-commit {head} to gh pr merge.")
    behind = commits_behind_base(repo, pr, head)
    if behind is None:
        deny("Could not compare the PR's head with its base; not merging unchecked.")
    if behind > 0:
        # A base update can invalidate checks that passed on the previous base.
        deny(f"The PR is {behind} commit(s) behind its base. Rebase onto the base, push "
             "(pre-push reruns the gate on the combined code), then merge the new head.")


def commits_behind_base(repo, pr, head):
    """How many base-branch commits the PR head lacks, or None when it cannot be read."""
    try:
        r = subprocess.run(["gh", "pr", "view", pr, "-R", repo, "--json", "baseRefName"],
                           capture_output=True, text=True, timeout=30)
        base = json.loads(r.stdout)["baseRefName"]
        r = subprocess.run(["gh", "api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".behind_by"],
                           capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip())
    except Exception:
        return None


CANON_HELP = "Run a PR merge as one standalone command, exactly: " \
             "`gh pr merge <n> -R <owner/repo> --squash --match-head-commit <approved-sha>` " \
             "(optional --delete-branch, no --auto, GH_TOKEN=... prefix allowed). No other merge path."


MERGE_PATH = (r"\bpr\b[^\n]*\bmerge\b|/pulls/[^/\s]+/merge|repos/[^\s]+/merges\b|merge-upstream"
              r"|mergePullRequest|mergeBranch|AutoMerge|enqueuePullRequest|updateRefs?\b"
              r"|createRef\b|deleteRef\b|createCommitOnBranch|\bgh\s+repo\s+sync\b")
# Endpoints a read may touch but a write may not: branch refs and file contents (a contents PUT commits to a branch).
WRITE_GUARDED = r"/git/refs\b|/contents(/|\b)"
MUTATE = (r"(-X|--method|--request)[\s=]*['\"]?(PATCH|POST|PUT|DELETE)"
          r"|\s(-f|-F|--field|--raw-field|--input|-d|--data[\w-]*|--json)(?=[\s=@]|\w)")
OPAQUE_BODY = (r"\s(--config|--post-file|--body-file|--upload-file)[\s=]|\s-[a-zA-Z]*[KT]|\s<\s*\S"
               r"|\|\s*(curl|http|https|wget|gh)\b")
MAX_PAYLOAD = 1_000_000
JQ_FORMATS = {"tsv", "csv", "json", "text", "base64", "base64d", "base32", "base32d", "sh", "html", "uri"}


def check_pr_merge(cmd):
    """Anything that mentions merging a PR must be the one canonical command."""
    if re.search(r"\bgraphql\b|api\.github\.com", cmd):
        # Payload files (gh -F q=@f / --input f, curl -d@f / --data* / --json @f) are read whole and checked like
        # inline text; stdin, oversized, unreadable and other opaque bodies are refused because they cannot be checked.
        unwrapped = re.sub(r"GH_TOKEN=\$\(gh auth token -u [\w-]+\)", "", cmd)
        if re.search(r"\$\(|`|\$\{?\w", unwrapped):
            deny("GitHub API request built from a shell expansion cannot be checked; write the body inline or as an @file.")
        if re.search(OPAQUE_BODY, cmd):
            deny("GitHub API request with a body from stdin, a config file or an upload cannot be checked; put the body inline or in an @file.")
        for f in re.findall(r"(?:[\s=]|-[a-zA-Z]*[dF])['\"]?@([^\s'\"]*)()|--input[\s=]+['\"]?([^\s'\"]+)", cmd):
            if f[0] in JQ_FORMATS:
                continue
            path = os.path.expanduser(f[0] or f[2])
            if path in ("", "-"):
                deny("GitHub API body from stdin cannot be checked; pass it inline or as a readable file.")
            try:
                if os.path.getsize(path) > MAX_PAYLOAD:
                    deny(f"GitHub API payload {path} is over {MAX_PAYLOAD} bytes, too large to check.")
                with open(path) as fh:
                    cmd = cmd + "\n" + fh.read()
            except OSError:
                deny(f"GitHub API payload {path} is not readable, so it cannot be checked for merge mutations.")
    merge_path = re.search(MERGE_PATH, cmd, re.I)
    ref_path = re.search(WRITE_GUARDED, cmd)
    if not (merge_path or ref_path):
        return
    if re.fullmatch(r"\s*gh\s+pr\s+merge\s+(--help|-h)\s*", cmd):
        return
    body = re.sub(r"^\s*GH_TOKEN=(\$\(gh auth token -u [\w-]+\)|[\w.-]+)\s+", "", cmd)
    if ref_path and not merge_path:
        # Reading a ref through a bare `gh api` is fine; any ref write through the API is refused.
        if re.match(r"\s*gh\s+api\s", body) and not OPERATORS.search(body) and not re.search(MUTATE, cmd, re.I):
            return
        deny("Branches change only through git push of a feature branch or the gated PR merge, never a ref or contents API write.")
    if OPERATORS.search(body):
        deny(CANON_HELP)
    try:
        argv = shlex.split(body)
    except ValueError:
        deny(CANON_HELP)
    if argv[:3] != ["gh", "pr", "merge"]:
        deny(CANON_HELP)
    rest, repo, pr, match, i = argv[3:], None, None, None, 0
    while i < len(rest):
        a = rest[i]
        if a in ("-R", "--repo", "--match-head-commit") and i + 1 < len(rest):
            v = rest[i + 1]; i += 2
            if a == "--match-head-commit":
                match = v
            else:
                repo = v
            continue
        if a in CANON_FLAGS:
            pass
        elif re.fullmatch(r"\d+", a) and pr is None:
            pr = a
        else:
            deny(CANON_HELP + f" Unexpected argument: {a!r}.")
        i += 1
    if not (repo and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and pr):
        deny(CANON_HELP)
    gate(repo, pr, match)


GIT_GLOBAL_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}
PUSH_VALUE = {"-o", "--push-option", "--receive-pack", "--exec", "--recurse-submodules"}
SHELLS = {"bash", "sh", "zsh", "fish", "dash"}


def tokens(cmd):
    cmd = cmd.replace("\n", " ; ")  # a newline ends a command
    # drop redirections like 2>&1, >/dev/null, > "/tmp/a b.log"
    cmd = re.sub(r"""(?<![^\s;&|])\d*(>>?|<)&?\s*("[^"]*"|'[^']*'|[^\s;&|()'"]*)""", " ", cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    return list(lex)


def check_push(cmd, depth=0):
    """git push must name a remote and a branch, never main/master, no wildcards."""
    if depth > 3 or not re.search(r"push", cmd):
        return
    # Backstop that does not depend on parsing quotes: decode \n-style escapes ($'...' bodies) and
    # refuse any visible `git ... push <remote> <main|master>` text.
    flat = cmd.replace("\\n", "\n").replace("\\t", " ")
    wrapped = re.search(r"\$'|\b(bash|sh|zsh|fish|dash|ssh|eval)\b", cmd)
    if wrapped and re.search(r"\bgit\b[^\n;&|]*?\bpush\b[^\n;&|]*?\s\+?([^\s:]*:)?(refs/heads/)?(" + "|".join(re.escape(b) for b in POLICY["protected_branches"] if b != "HEAD") + r")\b(?![-/\w])", flat):
        deny("No direct push to main/master: push a feature branch, open a PR, gate it, then gh pr merge.")
    for sub in re.findall(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`", cmd):  # command substitutions run too
        check_push(sub[0] or sub[1], depth + 1)
    try:
        toks = tokens(cmd)
    except ValueError:
        deny("Unparseable command that mentions push; write it plainly (`git push origin <branch>`).")
    for i, t in enumerate(toks):  # bodies that a wrapper executes: bash -c '...', ssh host '...'
        name = t.split("/")[-1]
        if name in SHELLS:
            # Do not model shell flags: every later argument that could be a script body is checked.
            for body in toks[i + 1:]:
                if "push" in body and (" " in body or "\t" in body):
                    check_push(body, depth + 1)
        elif name == "ssh":
            rest = [x for x in toks[i + 1:] if not set(x) <= set(";&|()<>")]
            hosts = [x for x in rest if not x.startswith("-")]
            if len(hosts) > 1:
                check_push(" ".join(hosts[1:]), depth + 1)
    for i, t in enumerate(toks):
        if t.split("/")[-1] != "git":
            continue
        j = i + 1
        while j < len(toks) and toks[j].startswith("-"):
            j += 2 if toks[j] in GIT_GLOBAL_VALUE else 1
        if j >= len(toks) or toks[j] != "push":
            continue
        args, k = [], j + 1
        while k < len(toks) and not set(toks[k]) <= set(";&|()<>"):
            args.append(toks[k]); k += 1
        pos, n = [], 0
        while n < len(args):
            x = args[n]
            if x == "--":
                pos += args[n + 1:]; break
            if x in ("--all", "--mirror", "--branches"):
                deny("git push --all/--mirror is refused; push one named branch.")
            if x in PUSH_VALUE:
                n += 2; continue
            # git accepts unique prefixes of long options (--recurse-submodule) and bundled short ones (-uo v)
            if x.startswith("--") and "=" not in x and len(x) > 4 and any(
                    v.startswith(x) for v in PUSH_VALUE if v.startswith("--")):
                n += 2; continue
            if re.fullmatch(r"-[a-zA-Z]+", x) and x.endswith("o"):
                n += 2; continue
            if x == "--repo" and n + 1 < len(args):
                pos.append(args[n + 1]); n += 2; continue
            if x.startswith("--repo="):
                pos.append(x.split("=", 1)[1]); n += 1; continue
            if not x.startswith("-"):
                pos.append(x)
            n += 1
        if len(pos) < 2:
            deny("git push must name the remote and the branch (`git push origin <branch>`); a bare push can hit main.")
        for ref in pos:
            if re.search(r"[$`{}]", ref):
                deny("git push needs a literal remote and branch; a shell expansion like $(git branch --show-current) can be main.")
        for ref in pos[1:]:
            src, _, dst = ref.lstrip("+").rpartition(":") if ":" in ref else ("", "", ref.lstrip("+"))
            dst = re.sub(r"^(refs/)?(heads/)?", "", dst or src)
            if not dst:
                deny("git push origin : pushes every matching branch, main included; name one branch.")
            if dst in POLICY["protected_branches"] or "*" in ref:
                deny("No direct push to main/master: push a feature branch, open a PR, gate it, then gh pr merge.")


def check_merge(cmd, cwd):
    cmd = re.sub(r"\\\r?\n", " ", cmd)  # join backslash-newline continuations
    check_pr_merge(cmd)
    check_push(cmd)


def check_delegate(inp):
    if "runtimeMode" in inp:
        deny("Never pass runtimeMode to delegate_task; a child awaiting approval can stall.")
    target = inp.get("target") or {}
    if not isinstance(target, dict) or not target.get("model") or not target.get("providerInstanceId"):
        deny("delegate_task needs an explicit target (providerInstanceId, model, options) so the seat caps can be checked.")
    model = target["model"]
    if model not in ALLOWED_MODELS:
        deny(f"Model {model} is not in the configured allowed_models.")
    opts = target.get("options") or {}
    if isinstance(opts, list):
        opts = {o.get("id"): o.get("value") for o in opts if isinstance(o, dict)}
    if not isinstance(opts, dict):
        deny("delegate_task options must be an object or a list of {id, value}.")
    efforts = [str(opts.get(k)).lower() for k in ("effort", "reasoningEffort", "reasoning_effort", "reasoning") if k in opts]
    if not efforts:
        deny("Set the effort explicitly; an inherited effort can exceed the configured cap.")
    if model not in POLICY["claude_models"]:
        bad = [e for e in efforts if e not in CODEX_EFFORTS]
        if bad:
            deny(f"Codex effort {bad[0]} refused; allowed efforts: {sorted(CODEX_EFFORTS)}.")
        if str(opts.get("serviceTier", POLICY["default_service_tier"])).lower() not in POLICY["codex_service_tiers"]:
            deny(f"Codex serviceTier must be one of {POLICY['codex_service_tiers']}.")
    for e in efforts:
        if e in BAD_EFFORT:
            deny(f"effort {e} is excluded by the configured cap.")


GATED = False


def main():
    global GATED
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = re.sub(r"\\\r?\n", " ", inp.get("command", ""))  # join backslash-newline continuations once, for every check
        if re.search(r"merge|push|/git/refs|updateRef|graphql|api\.github\.com|repo\s+sync|/contents", cmd, re.I):
            GATED = True
            check_merge(cmd, data.get("cwd"))
        if "acp-mcp-call" in cmd:
            # The ACP shell fallback calls the same T3 tools: run their checks on its tool name and JSON.
            GATED = True
            try:
                argv = tokens(cmd)
                calls = [k for k, a in enumerate(argv) if a.endswith("acp-mcp-call")]
                if not calls:
                    raise ValueError
                for i in calls:  # every invocation in a batch, not only the first
                    acp_tool = argv[i + 1]
                    if not re.fullmatch(r"\w+", acp_tool):
                        raise ValueError
                    acp_inp = json.loads(argv[i + 2]) if i + 2 < len(argv) and not set(argv[i + 2]) <= set(";&|()<>") else {}
                    check_t3_tool(acp_tool, acp_inp if isinstance(acp_inp, dict) else {})
            except (ValueError, IndexError, json.JSONDecodeError):
                deny("acp-mcp-call: write it as `acp-mcp-call <tool> '<json>'` with literal JSON so the T3 rules can be checked.")
        return
    check_t3_tool(tool.split("__")[-1], inp)


def check_t3_tool(name, inp):
    global GATED
    if name == "delegate_task":
        GATED = True
        check_delegate(inp)
    elif name in THREAD_TOOLS:
        fresh = os.path.exists(MARKER) and time.time() - os.path.getmtime(MARKER) < POLICY["thread_marker_seconds"]
        if not fresh:
            deny(f"Only the operator opens T3 threads. Use delegate_task. If the operator asked for a new thread, touch {MARKER} first.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # Fail closed only where a rule applies; everything else fails open.
        if GATED:
            deny(f"rules-guard error while checking a gated action ({e}); fix the hook or reshape the call.")
        print(f"rules-guard failed open: {e}", file=sys.stderr)
