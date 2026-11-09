# Configuration

The hooks read `guardrails.example.json` from the repository root unless `GUARDRAILS_CONFIG` names another file. The file must be complete. The hooks do not merge it with the example.

## Policy file

The policy file has five sections. Every key is required, because the hooks read them without defaults. The default column shows the value in `guardrails.example.json`.

| Option | Default | What it does |
| --- | --- | --- |
| `rules.allowed_models` | `claude-opus-5-5`, `gpt-6.1-sol`, `gpt-6-luna`, `gpt-6-astra` | Models a `delegate_task` target may use. |
| `rules.bad_effort` | `xhigh`, `max`, `ultra`, `ultracode`, `ultrathink` | Effort values refused for every model. |
| `rules.codex_efforts` | `low`, `medium` | Effort values allowed for models not in `claude_models`. |
| `rules.claude_models` | `claude-opus-5-5` | Models exempt from `codex_efforts` and the service tier check. |
| `rules.codex_service_tiers` | `priority`, `fast` | Allowed `serviceTier` for other models. |
| `rules.default_service_tier` | `priority` | Tier assumed when the target options have no `serviceTier`. |
| `rules.thread_tools` | `t3_thread_launch`, `create_threads`, `t3_thread_fork`, `t3_project_create` | T3 Code tools that open threads or projects. |
| `rules.thread_marker` | `~/.claude/state/operator-asked-thread` | Touch this file to allow a thread tool. |
| `rules.thread_marker_seconds` | `900` | How long the marker stays fresh. |
| `rules.protected_branches` | `main`, `master`, `HEAD` | Branches `git push` may not target. |
| `reply.max_lines` | `6` | Most non-empty lines in a reply to the operator. |
| `reply.max_chars` | `700` | Most characters in a reply to the operator. |
| `reply.reminder` | the reply rule | Text added to each prompt. Accepts `{max_lines}` and `{max_chars}`. |
| `reply.blocked` | the block message | Reason given when a reply is too long. Also accepts `{lines}` and `{chars}`. |
| `guarding.shared_checkout` | `~/agency` | Checkout where commits must name their paths. |
| `guarding.credential_stores` | `.codex/auth.json`, `.codex_work/auth.json`, `.claude/.credentials.json`, `.config/gh/hosts.yml`, `.pi/agent/auth.json` | Files under `~` that no read may touch. |
| `guarding.protected_hosts` | `production-host` | SSH host names and resolved `HostName` values to refuse. |
| `guarding.protected_proxy_pattern` | `\bboat proxy project-b\b` | Regular expression matched against the resolved `ProxyCommand`. |
| `guarding.credential_reason` | message | Reason for a refused credential read. |
| `guarding.commit_reason` | message | Reason for a refused commit. |
| `guarding.host_reason` | message | Reason for a refused SSH target. |
| `router.never_route` | `docs`, `humanizer`, `skill-creator` | Skill names the router never suggests. |
| `verdict.ledger` | `~/.claude/verdicts.tsv` | Ledger file for `verdict`. |
| `verdict.status_repositories` | empty | `owner/repo` prefixes where `verdict add` posts a commit status. |
| `verdict.github_user` | empty | `gh` account whose token posts the status. Empty uses the current login. |
| `verdict.github_users_by_prefix` | empty | Map of repository prefix to `gh` account, checked before `github_user`. |

Paths accept `~` and environment variables.

## Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `GUARDRAILS_CONFIG` | `guardrails.example.json` in the repository | Policy file for every hook and `verdict`. |
| `LOCAL_TZ` | `UTC` | Time zone for the local line of `now.sh`. |
| `OPENROUTER_API_KEY` | unset | Key for the Jev judge. Without it the router uses the Haiku daemon. |
| `SKILL_JUDGE` | `jev` | `jev`, `haiku`, or `union` (both judges, names combined). |
| `SKILL_JEV_MODEL` | `typesafe/jev-1.14-20261014` | Jev model on OpenRouter. |
| `SKILL_JEV_T` | `0.5` | Lowest Jev probability that routes a skill. |
| `SKILL_JUDGE_SOCK` | `~/.claude/state/skill-judge.sock` | Socket shared by the router and the daemon. |
| `SKILL_ROUTER_ASYNC` | set in `settings.example.json` | Emit the router's text as `additionalContext`, which an async hook needs. |
| `SKILL_JUDGE_PROVIDER` | `claude-cli` | Daemon backend: `claude-cli`, `anthropic` or `openai`. |
| `SKILL_JUDGE_MODEL` | `haiku` for `claude-cli` | Daemon model. API providers need one. |
| `SKILL_JUDGE_API_KEY` | unset | API key for the `anthropic` and `openai` providers. |
| `SKILL_JUDGE_BASE_URL` | the provider's public API | Base URL for a compatible API. |
| `SHORT_REPLY_FORCE` | unset | `1` makes `short-reply.py` treat the reader as the operator. The tests use it. |

## State files

The hooks keep their state in `~/.claude/state/`: `skill-pending/`, `skill-loaded/`, `skill-router-index.json`, `skill-router.log`, `skill-judge.sock` and `skill-judge.log`.
