#!/usr/bin/env python3
"""PreToolUse: hold edits and commands until every skill the router picked for this message is loaded.

Every Skill call is recorded per session (so the router does not ask again) and ticks its name off the pending set.
The first edit or command while names are still pending is denied with the missing list; the hold then clears, so
an agent that judges the rest off-subject is held exactly once.
"""
import json, os, re, sys, time

STATE = os.path.expanduser("~/.claude/state")
PENDING, LOADED = os.path.join(STATE, "skill-pending"), os.path.join(STATE, "skill-loaded")
HELD = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Agent", "Task", "mcp__t3-code__delegate_task",
        "mcp__t3_code__delegate_task"}
STALE = 3 * 3600


def load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(value, fh)
    os.replace(tmp, path)


def main():
    data = json.load(sys.stdin)
    sid = re.sub(r"[^\w-]", "", str(data.get("session_id") or ""))
    if not sid:
        return
    tool, pending_path = data.get("tool_name") or "", os.path.join(PENDING, sid)
    if tool == "Skill":
        name = str((data.get("tool_input") or {}).get("skill") or "")
        save(os.path.join(LOADED, sid), sorted(set(load(os.path.join(LOADED, sid), [])) | {name}))
        pending = load(pending_path, None)
        if pending:
            left = [n for n in pending.get("names", []) if n != name]
            if left:
                save(pending_path, {**pending, "names": left})
            else:
                os.unlink(pending_path)
        return
    if tool not in HELD or not os.path.exists(pending_path):
        return  # reading and searching stay free
    pending = load(pending_path, {})
    os.unlink(pending_path)
    if not pending.get("names") or time.time() - pending.get("at", 0) > STALE:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": (
            "skill gate: the router picked skills for this message that are not loaded yet: "
            f"{', '.join(pending['names'])}. Load the ones that apply with the Skill tool (all in one turn), then retry "
            "this call. This hold fires once; do not mention it or the skipped ones in your reply.")}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never block on a gate bug
