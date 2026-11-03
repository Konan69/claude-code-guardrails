"""Exercise installed entry points with unittest and replay incidents without live writes."""

import importlib.util
import json
import os
import shlex
import statistics
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("guarding", TOOLS / "guarding.py")
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
sys.path.insert(0, str(TOOLS))
SPEC.loader.exec_module(GUARD)


def invoke(script, data, force=True):
    """Use the same JSON stdin and stdout interface Claude uses, with a test-only reader override."""
    environment = dict(os.environ)
    environment.pop("SHORT_REPLY_FORCE", None)
    if force:
        environment["SHORT_REPLY_FORCE"] = "1"
    return subprocess.run(
        [sys.executable, *(["-S"] if sys.flags.no_site else []), str(TOOLS / script)], input=json.dumps(data).replace("$HOME", str(Path.home())), text=True,
        capture_output=True, env=environment, timeout=2, check=True,
    )


class Hooks(unittest.TestCase):
    """All fixtures live within the assigned tools directory and clean up after each test."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="u17-", dir=TOOLS)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.transcript = self.root / "transcript.jsonl"
        self.write_turn("Fix the issue.")

    def write_turn(self, prompt, extra=()):
        """Match Claude's compact JSONL, including real user messages and assistant tools."""
        rows = [{"type": "user", "message": {"content": prompt}}, *extra]
        self.transcript.write_text("\n".join(json.dumps(row, separators=(",", ":")) for row in rows))


    def bash(self, command, cwd=None):
        return invoke("guarding.py", {"cwd": str(cwd or Path.home() / "agency/project-b"),
                                      "tool_name": "Bash", "tool_input": {"command": command.replace("$HOME", str(Path.home()))}})

    def assert_denied(self, result, message):
        """An intentional refusal exits successfully and emits the supported hook decision."""
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(payload["permissionDecision"], "deny")
        self.assertIn(message, payload["permissionDecisionReason"])


    def test_shared_index_incident_4b23f184(self):
        for args in [["init", "-q"], ["config", "user.email", "u17@example.invalid"],
                     ["config", "user.name", "U17"]]:
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        (self.root / "owned").write_text("mine")
        (self.root / "foreign").write_text("another writer")
        subprocess.run(["git", "add", "owned", "foreign"], cwd=self.root, check=True)
        with mock.patch.object(GUARD, "AGENCY", self.root):
            denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                   "tool_input": {"command": "git commit -m x"}})
            self.assertIn("git commit -- <paths>", denied)
            self.assertIsNone(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                           "tool_input": {"command": "git commit -m x -- owned"}}))
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=self.root, text=True)
        self.assertEqual(staged.splitlines(), ["foreign", "owned"])
        self.assert_denied(self.bash("git commit -m x"), "git commit -- <paths>")

    def test_commit_variants(self):
        blocked = ["git commit --amend --no-edit", "git commit -a -m x -- own",
                   "git commit --all -- own", "git commit -am x -- own", "git commit --",
                   "git commit -m '--'", "git -C $HOME/agency/project-b commit -m x",
                   "cd $HOME/agency/project-b && git commit -m x",
                   "env FOO=1 command git commit -m x", "bash -c 'git commit -m x'",
                   "bash -lc 'git commit -m x'",
                   "(cd /tmp); git commit -m x"]
        for command in blocked:
            with self.subTest(command=command):
                self.assert_denied(self.bash(command), "git commit -- <paths>")
        allowed = ["git commit -m x -- own", "git commit --amend --no-edit -- own",
                   "git commit -mchange -- own",
                   "git commit -m '--all' -- own", "git status", "echo 'git commit -m x'",
                   "cd /tmp && git commit -m x", "git -C /tmp commit -m x"]
        for command in allowed:
            with self.subTest(command=command):
                self.assertEqual(self.bash(command).stdout, "")
        self.assert_denied(self.bash("git --git-dir=$HOME/agency/project-b/.git commit -m x", cwd=self.root),
                           "git commit -- <paths>")

    def test_bundled_commit_value_flags_consume_message(self):
        """Message and template values cannot masquerade as positional paths."""
        for arguments in ['-qm "fix"', '-nm "fix"', '-sm "fix" 2>&1', '-vm fix',
                          '-qmfix', '-qF -', '-qC HEAD', '-qc HEAD', '-qt template',
                          '--message=x', '--message x', '--file=-']:
            with self.subTest(arguments=arguments):
                self.assert_denied(self.bash("git commit " + arguments), "Shared index:")
                result = self.bash("git commit " + arguments + " -- a.py")
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        self.assert_denied(self.bash("git commit -qam fix -- a.py"), "Shared index:")
        self.assertEqual(self.bash("git commit -qma -- a.py").stdout, "")

    def test_launchers_cannot_hide_commands(self):
        for launcher in ["timeout 20", "timeout -s TERM -k 5 20", "timeout --signal=TERM 20",
                         "time", "time -p", "nice", "nice -n 5", "nohup",
                         "xargs -0 -r -n 1", "xargs --max-args=1", "stdbuf -oL -e 0",
                         "env -u UNUSED FOO=1", "command --", "exec",
                         "env FOO=1 timeout 20 nice -n 5 nohup"]:
            for command, message in [("git commit -m x", "Shared index:"),
                                     ("ssh production-host uptime", "Production server"),
                                     ("cat ~/.codex/auth.json", "Credentials never enter model context")]:
                with self.subTest(launcher=launcher, command=command):
                    self.assert_denied(self.bash(launcher + " " + command), message)
            result = self.bash(launcher + " git commit -m x -- a.py")
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_executable_substitutions_in_quotes_and_redirects(self):
        for source in ['out="$(git commit -m x 2>&1)"', 'out="`git commit -m x`"',
                       'echo "$(echo "$(git commit -m x)")"',
                       "echo $(git commit -m x)", "cat <(git commit -m x)",
                       "cat >(git commit -m x)", "diff <(echo a) <(echo b); git commit -m x",
                       "git commit -m x; diff <(echo a) <(echo b)"]:
            with self.subTest(source=source):
                self.assert_denied(self.bash(source), "Shared index:")
        for source in ["cat ~/.codex/auth.json <(true)", 'x="$(cat ~/.codex/auth.json)"',
                       'curl -H "Authorization: Bearer $(jq -r .x ~/.codex/auth.json)"']:
            self.assert_denied(self.bash(source), "Credentials never enter model context")
        self.assert_denied(self.bash("ssh production-host uptime <(true)"), "Production server")
        for source in ['echo \'$(git commit -m x)\'', 'echo \'`git commit -m x`\'',
                       "diff <(echo a) <(echo b)", "echo ok # $(git commit -m x)"]:
            result = self.bash(source)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
        self.assert_denied(self.bash("echo ok # comment\ngit commit -m x"), "Shared index:")

    def test_raw_credential_paths_and_globs_ignore_reader_and_lexer(self):
        for spelling in ["~/.codex/auth.json", "$HOME/.codex/auth.json", "${HOME}/.codex/auth.json",
                         "~/.codex/auth.*", "~/.codex/auth.jso?", "~/.codex/auth.[j]son"]:
            for reader in ["xxd", "od", "tac", "nl", "bat", "echo"]:
                with self.subTest(spelling=spelling, reader=reader):
                    self.assert_denied(self.bash(reader + " " + spelling), "Credentials never enter model context")
        self.assert_denied(self.bash('python3 -c "open(\'~/.codex/auth.json\')"'),
                           "Credentials never enter model context")
        for source in ["cat ~/.codex/auth.json 'unfinished", "echo $( ~/.codex/auth.json",
                       "ssh production-host 'unfinished"]:
            self.assert_denied(self.bash(source), "Credentials" if ".codex" in source else "Production")
        source = 'gh pr create --body "$(cat <<\'EOF\'\ncat ~/.codex/auth.json\nEOF\n)"'
        result = self.bash(source)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_denied(self.bash(source + "\ncat ~/.codex/auth.json"), "Credentials")

    def test_literal_production_hosts_never_spawn_resolution(self):
        for source in ["ssh production-host", "ssh user@production-host",
                       "scp -O -r file production-host:/tmp/file", "sftp production-host",
                       "sftp user@production-host:/tmp", "rsync x production-host:/tmp"]:
            with self.subTest(source=source), mock.patch.object(
                    subprocess, "run", side_effect=AssertionError("must not spawn")):
                denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                       "tool_input": {"command": source}})
                self.assertEqual(denied, GUARD.HOST_REASON)

    def test_resolution_timeouts_deny_each_transport(self):
        for source in ["ssh unresolved", "scp file unresolved:/tmp", "rsync file unresolved:/tmp",
                       "sftp unresolved"]:
            with self.subTest(source=source), mock.patch.object(
                    subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", .13)):
                denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                       "tool_input": {"command": source}})
                self.assertEqual(denied, "Could not resolve unresolved in time; retry.")
                self.assertNotIn("Production", denied)

    def test_worktree_operation_markers_need_no_git_process(self):
        """A worktree's gitdir file selects its own markers, including from a subdirectory."""
        checkout = self.root / "checkout"
        checkout.mkdir()
        metadata = self.root / "metadata"
        metadata.mkdir()
        (checkout / ".git").write_text("gitdir: ../metadata\n")
        subdirectory = checkout / "subdirectory"
        subdirectory.mkdir()
        (metadata / "MERGE_HEAD").write_text("0" * 40)
        sources = ["git commit --no-edit", f"git -C {checkout} commit --no-edit",
                   f"git --git-dir={metadata} commit --no-edit",
                   f"git --git-dir {metadata} commit --no-edit"]
        with mock.patch.object(GUARD, "AGENCY", self.root), mock.patch.object(
                subprocess, "run", side_effect=AssertionError("must not spawn")):
            for source in sources:
                with self.subTest(source=source):
                    self.assertIsNone(GUARD.reason({"cwd": str(subdirectory), "tool_name": "Bash",
                                                   "tool_input": {"command": source}}))
            (metadata / "MERGE_HEAD").unlink()
            self.assertIn("Shared index:", GUARD.reason({"cwd": str(subdirectory), "tool_name": "Bash",
                                                        "tool_input": {"command": "git commit -m x"}}))

    def test_named_descriptors_do_not_count_as_commit_paths(self):
        self.assert_denied(self.bash("git commit -m x {fd}>/tmp/log"), "Shared index:")
        result = self.bash("git commit -m x -- a.py {fd}>/tmp/log")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_bare_commits_with_redirections_are_denied(self):
        """Output files, descriptors, and heredoc delimiters never count as owned paths."""
        sources = [
            'git commit -m "msg" 2>&1 | tail -5',
            "git commit -m x > /tmp/log", "git commit -m x 2>/dev/null",
            "git commit -m x </dev/null", "git commit -m x <<< msg",
            "git commit -F - <<'EOF'\nmsg\nEOF",
            "git commit -m x >file", "git commit -m x >>file",
            "git commit -m x 2>>file", "git commit -m x <>file",
            "git commit -m x >&2", "git commit -m x &>file",
            "git commit -m x &>>file", "git commit -F - <<-EOF\n\tmsg\n\tEOF",
            "git commit -m x 2>&1|tail -5", "git commit -m x 2>&1>file",
            ">/tmp/log git commit -m x", "git commit -m x > 'log with spaces'",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assert_denied(self.bash(source), "Shared index:")

    def test_explicit_commit_paths_survive_redirections(self):
        """The same redirects remain allowed when a real pathspec is present."""
        for suffix in ["2>&1 | tail -3", "> /tmp/log", "2>/dev/null", "</dev/null",
                       "<<< msg", "<<'EOF'\nmsg\nEOF", ">file", ">>file", "2>>file",
                       "<>file", ">&2", "&>file", "&>>file", "<<-EOF\n\tmsg\n\tEOF"]:
            with self.subTest(suffix=suffix):
                result = self.bash("git commit -m x -- a.py " + suffix)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")
        for source in ["git commit -m x 2 >file", "git commit -m x -- '2' >file",
                       "git commit -m x -- '>'", "git commit -m x -- '2>&1'",
                       "git commit -m x -- a.py >file && echo ok"]:
            with self.subTest(source=source):
                result = self.bash(source)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")
        self.assert_denied(self.bash(
            "git commit -m x -- a.py 2>&1 | tail -3; git commit -m y >/tmp/log"
        ), "Shared index:")

    def test_credential_input_redirections_remain_denied(self):
        """Extracted input files still pass through the exact credential-store policy."""
        for source in ["cat < ~/.codex/auth.json", "cat 0<~/.codex/auth.json",
                       "cat <> ~/.codex/auth.json", "wc -l < ~/.codex/auth.json",
                       "< ~/.codex/auth.json", "bash -c true < ~/.codex/auth.json"]:
            with self.subTest(source=source):
                self.assert_denied(self.bash(source), "Credentials never enter model context")
        result = self.bash("cat < README.md")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_positional_commit_paths(self):
        for command in ["git commit -m x a.py", "git commit a.py -m x",
                        "git commit --amend --no-edit a.py", "git commit -mx a.py",
                        "git commit --message=x a.py"]:
            with self.subTest(command=command):
                result = self.bash(command)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        for command in ["git commit -a -m x a.py", "git commit a.py --all", "git commit -m a.py"]:
            self.assert_denied(self.bash(command), "git commit -- <paths>")

    def test_operation_commits_allow_pathless_and_keep_all_denied(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        with mock.patch.object(GUARD, "AGENCY", self.root):
            for marker in ["MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"]:
                path = Path(subprocess.check_output(
                    ["git", "rev-parse", "--path-format=absolute", "--git-path", marker],
                    cwd=self.root, text=True).strip())
                if marker.startswith("rebase-"):
                    path.mkdir()
                else:
                    path.write_text("0" * 40 + "\n")
                try:
                    command = f"git -C {self.root} commit --no-edit"
                    self.assertIsNone(GUARD.reason({"cwd": str(TOOLS), "tool_name": "Bash",
                                                   "tool_input": {"command": command.replace("$HOME", str(Path.home()))}}), marker)
                    self.assertIn("Shared index:", GUARD.reason({
                        "cwd": str(self.root), "tool_name": "Bash",
                        "tool_input": {"command": "git commit --all --no-edit"}}))
                finally:
                    path.rmdir() if path.is_dir() else path.unlink()
            denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                   "tool_input": {"command": "git commit --no-edit"}})
            self.assertIn("git merge --continue", denied)
            self.assertIn("git cherry-pick --continue", denied)

    def test_conflicted_merge_can_finish_with_real_git(self):
        def git(*args, check=True):
            return subprocess.run(["git", *args], cwd=self.root, text=True,
                                  capture_output=True, check=check)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "u17@example.invalid")
        git("config", "user.name", "U17")
        (self.root / "f").write_text("base\n")
        git("add", "f")
        git("commit", "-qm", "base")
        git("checkout", "-qb", "side")
        (self.root / "f").write_text("side\n")
        git("commit", "-qam", "side")
        git("checkout", "-q", "main")
        (self.root / "f").write_text("main\n")
        git("commit", "-qam", "main")
        self.assertEqual(git("merge", "side", check=False).returncode, 1)
        (self.root / "f").write_text("resolved\n")
        git("add", "f")
        self.assertIn("cannot do a partial commit during a merge",
                      git("commit", "--no-edit", "--", "f", check=False).stderr)
        with mock.patch.object(GUARD, "AGENCY", self.root):
            self.assertIsNone(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                           "tool_input": {"command": "git commit --no-edit"}}))
        git("commit", "--no-edit")
        self.assertEqual(len(git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()), 3)

    def test_heredoc_bodies_are_data_and_following_commands_are_checked(self):
        for opener, closer in [("EOF", "EOF"), ("'EOF'", "EOF"), ("-EOF", "\tEOF"),
                               ("-'EOF'", "\tEOF")]:
            source = f"cat > notes.md <<{opener}\nssh production-host is forbidden\ngit commit -m x\n{closer}"
            with self.subTest(opener=opener):
                result = self.bash(source)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                self.assert_denied(self.bash(source + "\ngit commit -m x"), "Shared index:")
        self.assertEqual(self.bash("cat <<A <<'B'\nssh production-host\nA\ngit commit -m x\nB").stdout, "")
        self.assert_denied(self.bash("cat <<EOF\nEOF suffix\nEOF\nssh production-host"),
                           "Production server")
        self.assert_denied(self.bash("echo '<<EOF'\ngit commit -m x"), "Shared index:")

    def test_command_substitution_heredoc_commit_paths(self):
        """Shell message bodies stay data while the enclosing commit retains its path rule."""
        for opener, closer in [("'EOF'", "EOF"), ('"EOF"', "EOF"),
                               ("EOF", "EOF"), ("-'EOF'", "\tEOF")]:
            for prefix in ["", "git add a.py && "]:
                source = (f'{prefix}git commit -m "$(cat <<{opener}\n'
                          f'Commit message\n\nssh production-host\n{closer}\n)"')
                with self.subTest(opener=opener, prefix=prefix):
                    self.assert_denied(self.bash(source), "Shared index:")
                    result = self.bash(source + " -- a.py")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(result.stdout, "")
        source = 'gh pr create --body "$(cat <<\'EOF\'\nSummary\ngit commit -m x\nEOF\n)"'
        result = self.bash(source)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")
        self.assert_denied(self.bash(source + "\ngit commit -m x"), "Shared index:")

    def test_remote_g_is_not_an_ssh_configuration_option(self):
        """Remote program flags cannot exempt a production connection from host resolution."""
        for source in ["ssh production-host ls -G", "ssh production-host -G",
                       "ssh -o BatchMode=yes production-host ls -G",
                       "ssh -- production-host ls -G",
                       "ssh -o SetEnv=NOTE=-G production-host ls"]:
            with self.subTest(source=source):
                self.assert_denied(self.bash(source), "Production server")
        result = self.bash("ssh ci-host ls -G")
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_ssh_configuration_only_is_allowed_without_resolution(self):
        with mock.patch.object(GUARD, "host_reason", side_effect=AssertionError("must not resolve")):
            for source in ["ssh -G production-host", "ssh -F config -G production-host"]:
                self.assertIsNone(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                               "tool_input": {"command": source}}))
        self.assertEqual(self.bash("ssh -G production-host").stdout, "")

    def test_ssh_g_in_no_value_bundles_is_configuration_only(self):
        """A pre-host G flag bypasses resolution even when bundled with verbosity flags."""
        for source in ["ssh -Gv production-host", "ssh -vG production-host",
                       "ssh -vvTG production-host", "ssh -F config -vG production-host"]:
            with self.subTest(source=source):
                with mock.patch.object(GUARD, "host_reason", side_effect=AssertionError("must not resolve")):
                    self.assertIsNone(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                                   "tool_input": {"command": source}}))
                result = self.bash(source)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout, "")
        self.assert_denied(self.bash("ssh production-host -vG"), "Production server")
        self.assert_denied(self.bash("ssh -vG production-host && ssh production-host"),
                           "Production server")

    def test_ssh_value_flags_consume_g_as_data(self):
        """getopt consumes the rest of a value-taking bundle rather than inspecting it for G."""
        for bundle, options in [("-pG", ["-p", "G"]), ("-vpG", ["-p", "G"]),
                                ("-vFG", ["-F", "G"]), ("-vEG", []), ("-vElogG", [])]:
            with self.subTest(bundle=bundle):
                self.assertEqual(GUARD.ssh_arguments(["ssh", bundle, "production-host"]),
                                 (options, ["production-host"]))
                with mock.patch.object(GUARD, "host_reason", return_value=GUARD.HOST_REASON) as resolve:
                    denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                           "tool_input": {"command": f"ssh {bundle} production-host"}})
                    self.assertEqual(denied, GUARD.HOST_REASON)
                    resolve.assert_called_once()
        self.assert_denied(self.bash("ssh -vEG production-host"), "Production server")
        self.assert_denied(self.bash("ssh -vE G production-host"), "Production server")

    def test_production_and_ci_alias_real_configuration(self):
        for command in ["ssh production-host uptime", "scp file production-host:/tmp/file",
                        "scp production-host:/tmp/file file", "scp -O -r file production-host:/tmp/file",
                        "rsync -a file production-host:/tmp/file", "ssh -o HostName=production-host ci-alias",
                        "ssh -o 'ProxyCommand=boat proxy project-b %p' ci-alias"]:
            with self.subTest(command=command):
                self.assert_denied(self.bash(command), "allowed CI host")
        for command in ["ssh ci-host uptime", "scp file ci-host:/tmp/file",
                        "rsync -a file ci-host:/tmp/file", "echo 'ssh production-host uptime'"]:
            with self.subTest(command=command):
                result = self.bash(command)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_resolved_alias_and_transfer_options(self):
        config = self.root / "sshconfig"
        config.write_text("Host hiddenprod\n HostName harmless\n ProxyCommand boat proxy project-b %p\n"
                          "Host allowed\n HostName ci-worker\n")
        for command in [f"ssh -F {config} hiddenprod", f"scp -F{config} x hiddenprod:/x",
                        f"rsync -e 'ssh -F {config}' x hiddenprod:/x",
                        f"rsync --rsh='ssh -F {config}' x hiddenprod:/x"]:
            self.assert_denied(self.bash(command), "Production server")
        self.assertEqual(self.bash(f"ssh -F {config} allowed").stdout, "")

    def test_credential_incident_and_allowed_reads(self):
        for store in GUARD.STORES:
            for command in [f"cat ~/{store}", f"head -c 0 $HOME/{store}",
                            f"less $HOME/{store}", f"cat < ~/{store}"]:
                with self.subTest(command=command):
                    self.assert_denied(self.bash(command), "Credentials never enter model context")
            data = {"cwd": str(self.root), "tool_name": "Read", "tool_input": {"file_path": f"~/{store}"}}
            self.assert_denied(invoke("guarding.py", data), "Credentials never enter model context")
        link = self.root / "indirect"
        link.symlink_to(Path.home() / ".codex/auth.json")
        self.assert_denied(self.bash(f"cat {link}"), "Credentials never enter model context")
        for command in ["cat README.md", "head -20 settings.json"]:
            self.assertEqual(self.bash(command).stdout, "")
        data = {"cwd": str(self.root), "tool_name": "Read", "tool_input": {"file_path": "README.md"}}
        self.assertEqual(invoke("guarding.py", data).stdout, "")


    def test_hash_inside_word_does_not_start_a_comment(self):
        self.assert_denied(self.bash("echo word#suffix; git commit -m x"), "Shared index:")
        self.assert_denied(self.bash("ssh -o x#y production-host"), "Production server")
        self.assert_denied(self.bash("git commit -m fix#1"), "Shared index:")
        result = self.bash("git commit -m fix#1 -- a.py")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_three_ci_destinations_use_real_ssh_configuration(self):
        source = ("scp a ci-worker3:/tmp/ && ssh ci-worker3 ls /tmp/a; "
                  "rsync -az a/ ci-worker2:/tmp/a/ && ssh ci-worker2 ls; "
                  "ssh ci-worker uptime; ssh ci-host uptime")
        result = self.bash(source)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_resolution_cache_keys_include_options_and_host(self):
        source = ("scp a ci:/tmp/ && ssh ci ls; ssh -o HostName=other ci uptime; "
                  "ssh -o HostName=other ci ls; ssh other uptime")
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "hostname ci\n", "")) as run:
            self.assertIsNone(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash",
                                           "tool_input": {"command": source}}))
        self.assertEqual(run.call_count, 3)
        for call in run.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0.3)
            self.assertLessEqual(call.kwargs["timeout"], 0.4)

    def test_resolution_total_budget_and_literal_host_after_cache(self):
        with mock.patch.object(GUARD.time, "monotonic", side_effect=[0, .1, .5, .9, 1.21]), mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "hostname ci\n", "")) as run:
            denied = GUARD.reason({"cwd": str(self.root), "tool_name": "Bash", "tool_input": {
                "command": "ssh a; ssh b; ssh c; ssh d"}})
        self.assertEqual(denied, "Could not resolve d in time; retry.")
        self.assertEqual(run.call_count, 3)
        self.assertAlmostEqual(run.call_args_list[-1].kwargs["timeout"], .3)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "hostname ci\n", "")) as run:
            self.assertEqual(GUARD.reason({"cwd": str(self.root), "tool_name": "Bash", "tool_input": {
                "command": "ssh ci; ssh production-host"}}), GUARD.HOST_REASON)
        self.assertEqual(run.call_count, 1)

    def test_shell_reserved_words_do_not_hide_commands(self):
        for source in ["if true; then git commit -m x; fi",
                       "if git diff --quiet; then :; else git commit -m x; fi",
                       "{ git commit -m x; } 2>&1 | tail", "! git commit -m x",
                       "while true; do git commit -m x; break; done",
                       "until git commit -m x; do break; done", "(git commit -m x)"]:
            with self.subTest(source=source):
                self.assert_denied(self.bash(source), "Shared index:")
        self.assert_denied(self.bash("for h in a; do ssh production-host uptime; done"), "Production")
        for source in ["if true; then git commit -m x -- own; fi", "{ git commit -m x -- own; }"]:
            result = self.bash(source)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_node_home_prefix_is_a_literal_credential_path(self):
        for source in [
            "node -e \"console.log(require('fs').readFileSync(process.env.HOME+'/.codex/auth.json','utf8'))\"",
            "bun -e \"Bun.file(process.env.HOME + '/.codex_work/auth.json').text()\"",
        ]:
            self.assert_denied(self.bash(source), "Credentials")

    def test_metadata_and_message_mentions_are_allowed(self):
        for source in ["du -sh ~/.claude/* | sort -h | tail", "ls -la ~/.config/*", "ls ~/*",
                       "ls -la ~/.codex/auth.json", "test -f ~/.codex/auth.json && echo present",
                       "[ -f ~/.codex/auth.json ]", "stat -c %s ~/.codex/auth.json",
                       "file ~/.codex/auth.json", "realpath ~/.codex/auth.json", "readlink ~/.codex/auth.json",
                       "rg -n '~/.codex/auth.json' ~/.claude/tools", "rg 'auth.json' .",
                       "rg -e~/.codex/auth.json ~/.claude/tools",
                       "grep -n -e '~/.codex/auth.json' ~/.claude/tools/guarding.py",
                       'git commit -m "Deny reads of ~/.codex/auth.json" -- a.py',
                       'git commit -qm "Deny reads of ~/.codex/auth.json" -- a.py']:
            with self.subTest(source=source):
                result = self.bash(source)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        for source in ["cat ~/.codex/auth.*", "cat ~/.*/*", "wc -c ~/.codex/auth.json",
                       "rg auth ~/.codex/auth.json", "grep -e auth ~/.codex/auth.json",
                       "rg -f ~/.codex/auth.json .", "rg -f~/.codex/auth.json .",
                       "grep -nf~/.codex/auth.json .", "rg -m1 auth ~/.codex/auth.json",
                       "ls -la < ~/.codex/auth.json"]:
            self.assert_denied(self.bash(source), "Credentials")
        for source in ["cat ~/*", "cat ~/.config/*", "cat ~/.claude/*"]:
            result = self.bash(source)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_interpreter_heredocs_execute_while_document_bodies_stay_data(self):
        scripts = [
            ("python3 -", "import json\njson.load(open('$HOME/.codex/auth.json'))"),
            ("python3", "import os\nopen(os.path.expanduser('~/.claude/.credentials.json')).read()"),
            ("node", "require('fs').readFileSync('$HOME/.codex/auth.json')"),
            ("bun", "Bun.file('$HOME/.codex/auth.json').text()"),
            ("deno", "Deno.readTextFile('$HOME/.codex/auth.json')"),
            ("ruby", "File.read('$HOME/.codex/auth.json')"),
            ("perl", "open(F, '$HOME/.codex/auth.json')"),
            ("bash", "cat ~/.codex/auth.json"), ("sh", "cat ~/.codex/auth.json"),
            ("zsh", "cat ~/.codex/auth.json"), ("env X=1 python3 -", "open('$HOME/.codex/auth.json')"),
        ]
        for launcher, body in scripts:
            with self.subTest(launcher=launcher):
                self.assert_denied(self.bash(launcher + " <<'EOF'\n" + body + "\nEOF"), "Credentials")
        self.assert_denied(self.bash("bash <<'EOF'\ngit commit -m x\nEOF"), "Shared index:")
        self.assert_denied(self.bash("bash <<'EOF'\nssh production-host\nEOF"), "Production")
        self.assert_denied(self.bash("python3 - <<-'EOF'\n\topen('$HOME/.codex/auth.json')\n\tEOF"), "Credentials")
        for launcher in ["gh pr create --body-file -", "git commit -F - -- own", "cat", "tee /tmp/doc"]:
            result = self.bash(launcher + " <<'EOF'\ncat ~/.codex/auth.json\nssh production-host\ngit commit -m x\nEOF")
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
        source = "cat <<'EOF'\ndata\nEOF\npython3 - <<'EOF'\nopen('$HOME/.codex/auth.json')\nEOF"
        self.assert_denied(self.bash(source), "Credentials")
        result = self.bash("bash <<'EOF'\nls -la ~/.codex/auth.json\nEOF")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_whole_worktree_pathspecs_are_refused(self):
        for path in [".", "./", ":/", ":", "*"]:
            for separator in [" ", " -- "]:
                with self.subTest(path=path, separator=separator):
                    self.assert_denied(self.bash("git commit -m x" + separator + path), "Shared index:")
        (self.root / ".git").mkdir()
        child = self.root / "child"
        child.mkdir()
        with mock.patch.object(GUARD, "AGENCY", self.root):
            self.assertEqual(GUARD.reason({"cwd": str(child), "tool_name": "Bash", "tool_input": {
                "command": "git commit -m x -- " + str(self.root)}}), GUARD.COMMIT_REASON)

    def test_lock_and_scheduler_launchers_do_not_hide_commands(self):
        self.assert_denied(self.bash("flock /tmp/l -c 'git commit -m x'"), "Shared index:")
        for launcher in ["flock /tmp/l", "flock -x -w 2 /tmp/l", "flock --timeout=2 /tmp/l",
                         "flock -E 3 /tmp/l", "flock 9", "ionice -c 3", "ionice -c 2 -n 7",
                         "taskset 0x1", "taskset -c 0", "taskset --cpu-list 0", "chronic",
                         "watch -n 2", "env X=1 flock /tmp/l ionice -c 3 taskset -c 0 chronic"]:
            for source, marker in [("git commit -m x", "Shared index:"),
                                   ("ssh production-host uptime", "Production"),
                                   ("cat ~/.codex/auth.json", "Credentials")]:
                with self.subTest(launcher=launcher, source=source):
                    self.assert_denied(self.bash(launcher + " " + source), marker)
            result = self.bash(launcher + " git commit -m x -- own")
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")


    def test_hooks_agree_without_site_imports(self):
        for script, payload in [
            ("guarding.py", {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}),
            ("guarding.py", {"tool_name": "Bash", "tool_input": {"command": "ssh production-host"}}),
            ("guarding.py", {"tool_name": "Bash", "tool_input": {"command": "ssh ci-host"}}),
            ("guarding.py", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.codex/auth.json"}}),
            ("guarding.py", {"tool_name": "Bash", "tool_input": {"command": "ls ~/.codex/auth.json"}}),
        ]:
            payload = {"cwd": str(Path.home() / "agency/project-b"), **payload}
            expected = invoke(script, payload)
            result = subprocess.run([sys.executable, "-S", str(TOOLS / script)], input=json.dumps(payload),
                                    text=True, capture_output=True, check=True, timeout=2,
                                    env={**os.environ, "SHORT_REPLY_FORCE": "1"})
            self.assertEqual(result.stdout, expected.stdout)
            self.assertEqual(result.stderr, expected.stderr)


if __name__ == "__main__":
    unittest.main()
