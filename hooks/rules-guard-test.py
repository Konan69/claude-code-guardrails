#!/usr/bin/env python3
"""Probe rules-guard.py: each case is (tool_name, tool_input, expected 'allow'|'deny')."""
import json, subprocess, sys, os
from pathlib import Path

G = str(Path(__file__).with_name("rules-guard.py"))
S = "9575b9cac7b62bb0f2945ff793da025d6e4de99f"
B = "Bash"
D = "mcp__t3-code__delegate_task"
OPUS = {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5"}
SOL = {"providerInstanceId": "codex_work", "model": "gpt-6.1-sol"}

CASES = [
    (B, f"gh pr merge 66 -R example/project --squash --match-head-commit {S}", "allow"),
    (B, f"GH_TOKEN=$(gh auth token -u example-owner) gh pr merge 66 -R example/project --squash --match-head-commit {S}", "allow"),
    (B, "gh pr merge --help", "allow"),
    (B, "gh pr view 64 -R example/project --json state,mergeCommit", "allow"),
    (B, "git push -u origin feat/x", "allow"),
    (B, "git -C ~/r push origin HEAD:feat/x", "allow"),
    (B, "git commit -m x -- a.py && git push origin feat/x", "allow"),
    (B, "git merge --ff-only origin/main", "allow"),
    (B, "gh pr merge 66 -R example/project --squash", "deny"),
    (B, f"gh pr merge 64 -R example/project --squash --match-head-commit {S}", "deny"),
    (B, "/usr/bin/gh pr merge 66 -R example/project -s", "deny"),
    (B, "gh -R example/project pr merge 66 -s", "deny"),
    (B, "env FOO=1 bash -c 'gh pr merge 66 -R example/project -s'", "deny"),
    (B, "if true; then gh pr merge 66 -R example/project -s; fi", "deny"),
    (B, "gh pr merge 66 -R example/project -s --body --help", "deny"),
    (B, f"gh pr merge --subject 66 64 -R example/project --match-head-commit {S}", "deny"),
    (B, "env GH_REPO=example/project gh pr merge 64 --squash", "deny"),
    (B, "gh api -X PUT repos/example/project/pulls/66/merge?foo=bar", "deny"),
    (B, "gh api graphql -f query='mutation{mergePullRequest(input:{})}'", "deny"),
    (B, "git push origin +main", "deny"),
    (B, "git push origin HEAD:main", "deny"),
    (B, "git push origin 'refs/*:refs/*'", "deny"),
    (B, "git push origin", "deny"),
    (B, "git push", "deny"),
    (B, "gh pr \\\n merge 64 -R example/project --squash", "deny"),
    (B, 'bash -c "git push origin main"', "deny"),
    (B, "ssh ci-worker 'cd r && git push origin main'", "deny"),
    (B, "git push -o ci.skip origin", "deny"),
    (B, "git -C /tmp/r push origin feat/y", "allow"),
    (B, "git remote get-url --push origin", "allow"),
    (B, "git config --get remote.origin.push", "allow"),
    (B, "git push origin feat/x -- ", "allow"),
    (B, "git push origin\ngit status", "deny"),
    (B, "git --config-env http.extraHeader=GIT_AUTH_HEADER push origin main", "deny"),
    (B, "git push --repo origin feat/x", "allow"),
    (B, "git push --repo=origin feat/x", "allow"),
    (B, 'git commit -m "Document git push origin main guard" -- a.md', "allow"),
    (B, "ssh -p 22 ci-worker git push origin main", "deny"),
    (B, "ssh ci-worker 'git push origin feat/x'", "allow"),
    (B, "git push origin 2>&1", "deny"),
    (B, "git push origin > /tmp/log 2>&1", "deny"),
    (B, "git push origin feat/x 2>&1 | tail -3", "allow"),
    (B, "git push --signed origin feat/x", "allow"),
    (B, "git push origin :", "deny"),
    (B, "bash -e -c 'git push origin main'", "deny"),
    (B, "bash -euc 'git push origin main'", "deny"),
    (B, "bash -o pipefail -c 'git push origin main'", "deny"),
    (B, "ssh ci-worker \"bash -euc 'git push origin main'\"", "deny"),
    (B, "bash -euc 'git push origin feat/x'", "allow"),
    (B, "bash +e -c 'git push origin main'", "deny"),
    (B, "bash +o errexit -c 'git push origin main'", "deny"),
    (B, "ssh ci-worker \"bash +e -c 'git push origin main'\"", "deny"),
    (B, "bash +e -c 'git push origin feat/x'", "allow"),
    (B, "bash -c $'set -e\\ngit push origin main'", "deny"),
    (B, "bash -c $'set -e\\ngit push origin feat/main-fix'", "allow"),
    (B, "gh api repos/example/project/git/refs/heads/main -q .object.sha", "allow"),
    (B, "gh api repos/example/project/git/refs/heads/feat/x", "allow"),
    (B, "gh api graphql -F query=@__FIXTURES__/merge.graphql", "deny"),
    (B, "gh api graphql --input __FIXTURES__/merge.graphql", "deny"),
    (B, "gh api graphql -F query=@__FIXTURES__/read.graphql", "allow"),
    (B, "gh api graphql -F query=@__FIXTURES__/missing.graphql", "deny"),
    (B, "gh api -X POST repos/o/r/git/refs -f ref=refs/heads/main -f sha=abc", "deny"),
    (B, "curl https://api.github.com/graphql --data-binary @__FIXTURES__/merge.json", "deny"),
    (B, "curl https://api.github.com/graphql -d @__FIXTURES__/merge.json", "deny"),
    (B, "curl https://api.github.com/graphql --json @__FIXTURES__/merge.json", "deny"),
    (B, "cat q.json | curl https://api.github.com/graphql -d @-", "deny"),
    (B, "curl https://api.github.com/graphql -d@__FIXTURES__/merge.json", "deny"),
    (B, "curl https://api.github.com/graphql -d@-", "deny"),
    (B, "curl --config req.cfg https://api.github.com/graphql", "deny"),
    (B, "http POST https://api.github.com/graphql < body.json", "deny"),
    (B, "wget --post-file=body.json https://api.github.com/graphql", "deny"),
    (B, "gh api repos/o/r/git/refs -fref=refs/heads/main -fsha=abc", "deny"),
    (B, "gh repo sync o/r --branch main", "deny"),
    (B, "gh api repos/o/r/merge-upstream -f branch=main", "deny"),
    (B, "gh api graphql -f query='mutation{enqueuePullRequest(input:{})}'", "deny"),
    (B, "gh api graphql -f query='mutation{createCommitOnBranch(input:{})}'", "deny"),
    (B, "gh api graphql -f query='mutation{createRef(input:{})}'", "deny"),
    (B, "gh api -X PUT repos/o/r/contents/README.md -f message=x -f content=eA==", "deny"),
    (B, "gh api repos/o/r/contents/README.md -q .sha", "allow"),
    (B, "git push --recurse-submodules check origin", "deny"),
    (B, "git push origin HEAD:heads/main", "deny"),
    (B, "git push origin {main,feat/x}", "deny"),
    (B, "git push --recurse-submodules=check origin feat/x", "allow"),
    (B, "curl -sSd@__FIXTURES__/merge.json https://api.github.com/graphql", "deny"),
    (B, "curl -Kcfg https://api.github.com/graphql", "deny"),
    (B, "curl -sTbody.json https://api.github.com/graphql", "deny"),
    (B, 'result="$(git push origin main)"', "deny"),
    (B, "out=`git push origin main`", "deny"),
    (B, 'result="$(git push origin feat/x 2>&1)"', "allow"),
    (B, "git push -uo ci.skip origin", "deny"),
    (B, "git push --recurse-submodule check origin", "deny"),
    (B, "git push -u origin feat/x", "allow"),
    (B, "gh api repos/example/project/pulls -q '.[] | [.number,.title] | @tsv'", "allow"),
    (B, 'git push origin > "/tmp/push.log"', "deny"),
    (B, """ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" acp-mcp-call delegate_task '{"task":"x","target":{"providerInstanceId":"codex","model":"gpt-6.1-sol","options":{"reasoningEffort":"high"}}}'""", "deny"),
    (B, """ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" acp-mcp-call delegate_task '{"task":"x","target":{"providerInstanceId":"claudeAgent","model":"claude-opus-5-5","options":{"effort":"max"}}}'""", "deny"),
    (B, """ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" acp-mcp-call t3_thread_launch '{"title":"x"}'""", "deny"),
    (B, """ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" acp-mcp-call delegate_task '{"task":"x","target":{"providerInstanceId":"codex_work","model":"gpt-6.1-sol","options":{"reasoningEffort":"high","serviceTier":"priority"}}}'""", "deny"),
    (B, """ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" acp-mcp-call orchestrator_capabilities '{}'""", "allow"),
    (B, """node x acp-mcp-call delegate_task "$(cat brief.json)\"""", "deny"),
    (B, """node x acp-mcp-call orchestrator_capabilities '{}' && node x acp-mcp-call delegate_task '{"task":"x","target":{"providerInstanceId":"codex","model":"gpt-6.1-sol","options":{"reasoningEffort":"high"}}}'""", "deny"),
    (B, """node x acp-mcp-call orchestrator_capabilities '{}'\nnode x acp-mcp-call t3_thread_launch '{"title":"x"}'""", "deny"),
    (B, """node x acp-mcp-call orchestrator_capabilities '{}' && node x acp-mcp-call task_status '{"taskId":"t"}'""", "allow"),
    (B, """node x acp-mcp-call \\\n delegate_task '{"task":"x","target":{"providerInstanceId":"codex","model":"gpt-6.1-sol","options":{"reasoningEffort":"high"}}}'""", "deny"),
    (B, """node x acp-mcp-call \\\n t3_thread_launch '{"title":"x"}'""", "deny"),
    (B, """node x acp-mcp-call \\\n task_status \\\n '{"taskId":"t"}'""", "allow"),
    (B, 'git push origin feat/x > "/tmp/push log.txt" 2>&1', "allow"),
    (B, 'gh api graphql -f query="$(cat __FIXTURES__/merge.graphql)"', "deny"),
    (B, 'curl https://api.github.com/graphql -d "$(cat body.json)"', "deny"),
    (B, 'GH_TOKEN=$(gh auth token -u example-user) gh api graphql -f query="{viewer{login}}"', "allow"),
    (B, "curl https://api.github.com/graphql --json @__FIXTURES__/read.graphql", "allow"),
    (B, 'BASE=main; gh api -X PATCH "repos/o/r/git/refs/heads/$BASE" -f sha=abc', "deny"),
    (B, "gh api -X PATCH repos/o/r/git/refs/heads/feat/x -f sha=abc", "deny"),
    (B, "gh api graphql -f query='mutation{mergeBranch(input:{})}'", "deny"),
    (B, "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{})}'", "deny"),
    (B, "gh api graphql -f query='{repository(owner:\"o\",name:\"r\"){pullRequest(number:1){state}}}'", "allow"),
    (B, "curl --request PATCH --data '{\"sha\":\"x\"}' https://api.github.com/repos/example/project/git/refs/heads/main", "deny"),
    (B, "curl -s https://api.github.com/repos/example/project/git/refs/heads/main", "deny"),
    (B, "gh api repos/example/project/git/refs/heads/main | jq .object", "deny"),
    (B, "git push origin feat/x && git log main --oneline -1", "allow"),
    (B, "sh --norc -c 'git push origin master'", "deny"),
    (B, "ssh ci-worker \"bash -e -c 'git push origin feat/x'\"", "allow"),
    (B, f"gh pr merge 66 -R example/project --squash --auto --match-head-commit {S}", "deny"),
    (B, f"gh pr merge 66 -R example/project --squash --admin --match-head-commit {S}", "deny"),
    (B, "gh api --method POST repos/example/project/merges -f base=main -f head=feat/x", "deny"),
    (B, "gh api -X PATCH repos/example/project/git/refs/heads/main -f sha=abc", "deny"),
    (B, "gh api repos/example/project/commits/main -q .sha", "allow"),
    (B, "git push origin +:", "deny"),
    (B, "git push origin main:", "deny"),
    (B, "git push origin feat/x:feat/x", "allow"),
    (B, "git push origin --delete feat/old", "allow"),
    (B, 'git push origin "$(git branch --show-current)"', "deny"),
    (B, "git push -u origin `git branch --show-current`", "deny"),
    (B, 'PR=66; gh api -X PUT "repos/example/project/pulls/$PR/merge"', "deny"),
    (B, "git push --signed=if-asked origin main", "deny"),
    (D, {"task": "x", "target": {**OPUS, "options": {"effort": "max"}}}, "deny"),
    (D, {"task": "x", "target": {**OPUS, "options": [{"id": "effort", "value": "max"}]}}, "deny"),
    (D, {"task": "x", "target": {**OPUS, "options": [{"id": "effort", "value": "high"}]}}, "allow"),
    (D, {"task": "x", "target": {**SOL, "options": {"reasoningEffort": "high", "serviceTier": "priority"}}}, "deny"),
    (D, {"task": "x", "target": {**OPUS, "options": {}}}, "deny"),
    (D, {"task": "x"}, "deny"),
    (D, {"task": "x", "runtimeMode": "full-access", "target": {**SOL, "options": {"reasoningEffort": "high"}}}, "deny"),
    (D, {"task": "x", "target": {"providerInstanceId": "codex", "model": "gpt-6.1-sol", "options": {"reasoningEffort": "high"}}}, "deny"),
    (D, {"task": "x", "target": {"providerInstanceId": "claudeAgent", "model": "claude-opus-4-8", "options": {"effort": "high"}}}, "deny"),
    ("mcp__t3-code__t3_thread_launch", {"title": "x"}, "deny"),
]

RUNNER = r'''import json, runpy, subprocess, sys
real_run = subprocess.run

def local_gh(argv, **kwargs):
    if argv[0] != "gh":
        return real_run(argv, **kwargs)
    if argv[1] == "api":
        output = "0"
    else:
        output = json.dumps({"headRefOid": "9575b9cac7b62bb0f2945ff793da025d6e4de99f" if argv[3] == "66" else "0" * 40,
                             "baseRefName": "main"})
    return subprocess.CompletedProcess(argv, 0, output, "")

subprocess.run = local_gh
sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[1]).parent))
runpy.run_path(sys.argv[1], run_name="__main__")
'''

fails = 0
for tool, inp, want in CASES:
    if tool == B:
        inp = {"command": inp.replace("__FIXTURES__", str(Path(__file__).with_name("rules-guard-fixtures")))}
    out = subprocess.run([sys.executable, "-c", RUNNER, G], input=json.dumps({"tool_name": tool, "tool_input": inp}),
                         capture_output=True, text=True, check=True).stdout
    got = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else "allow"
    ok = got == want
    fails += not ok
    if not ok or "-v" in sys.argv:
        print(f"{'ok  ' if ok else 'FAIL'} want={want} got={got} {tool.split('__')[-1]} {str(inp)[:90]}")
print(f"{len(CASES) - fails}/{len(CASES)} pass")
sys.exit(1 if fails else 0)
