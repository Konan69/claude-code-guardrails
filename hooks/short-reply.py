#!/usr/bin/env python3
"""Reply-length rule as hooks. `remind` runs on UserPromptSubmit, `check` on Stop.

The rule covers text the operator reads. When the reader is an agent (a T3 delegated child,
a headless `claude -p` run, or a brief marked `READER: agent`) both modes stand down.
"""
import json, os, re, subprocess, sys

from policy import CONFIG
POLICY = CONFIG["reply"]
MAX_LINES, MAX_CHARS = POLICY["max_lines"], POLICY["max_chars"]
TAIL_BYTES = 4_000_000
T3_CHILD = re.compile(r"^\s*Act as the [\w -]{1,40}sub-agent", re.I)
MARKER = re.compile(r"^\s*READER:\s*agent\b", re.I | re.M)
def _prompt_text(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if d.get("type") != "user" or d.get("isMeta") or d.get("isSidechain"):
        return None
    c = d.get("message", {}).get("content")
    if isinstance(c, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
            return None
        c = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c if isinstance(c, str) and c.strip() else None


def prompts(path):
    """First and latest real user prompt in the transcript, without reading all of it."""
    first = last = None
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"type":"user"' not in raw:
                    continue
                first = _prompt_text(raw.decode("utf-8", "ignore"))
                if first:
                    break
            size = f.seek(0, 2)
            f.seek(max(0, size - TAIL_BYTES))
            for raw in f.read().splitlines()[1 if size > TAIL_BYTES else 0:]:
                if b'"type":"user"' in raw:
                    last = _prompt_text(raw.decode("utf-8", "ignore")) or last
    except (OSError, TypeError):
        pass
    return [p for p in (first, last) if p]


def last_reply(data):
    text = data.get("last_assistant_message")
    if isinstance(text, str):
        return text
    text = ""
    try:
        with open(data["transcript_path"], errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant" or d.get("isSidechain"):
                    continue
                parts = d.get("message", {}).get("content", [])
                t = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
                if t.strip():
                    text = t
    except (KeyError, OSError):
        pass
    return text


def reader_is_agent(data):
    if os.environ.get("SHORT_REPLY_FORCE") == "1":
        return False
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "sdk-cli":
        return True
    seen = prompts(data.get("transcript_path"))
    if isinstance(data.get("prompt"), str):
        seen.append(data["prompt"])
    if not seen:
        return False
    first, current = seen[0], seen[-1]
    return bool(T3_CHILD.search(first) or MARKER.search(first) or MARKER.search(current))


def remind(data):
    if reader_is_agent(data):
        print("reader: an agent, not the operator. The short-reply rule is off for this turn: report in as much detail as the reader needs.")
        return
    print(POLICY["reminder"].format(max_lines=MAX_LINES, max_chars=MAX_CHARS))


def check(data):
    if data.get("stop_hook_active") or reader_is_agent(data):
        return
    text = last_reply(data).strip()
    if not text:
        return
    lines = sum(1 for l in text.splitlines() if l.strip())
    if lines <= MAX_LINES and len(text) <= MAX_CHARS:
        return
    print(json.dumps({
        "decision": "block",
        "reason": POLICY["blocked"].format(lines=lines, chars=len(text), max_lines=MAX_LINES, max_chars=MAX_CHARS),
    }))


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    (remind if sys.argv[1:] == ["remind"] else check)(data)


main()
