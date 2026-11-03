#!/usr/bin/env python3
"""Skill judge over a Unix socket. Default provider uses the Claude CLI login.

SKILL_JUDGE_PROVIDER selects claude-cli, anthropic or openai.
SKILL_JUDGE_MODEL defaults to haiku for claude-cli. API providers require a model.
SKILL_JUDGE_API_KEY supplies the API credential, never read from disk.
SKILL_JUDGE_BASE_URL overrides the API base URL for compatible providers.
SKILL_JUDGE_SOCK overrides the Unix socket path, shared with skill-router.py.
"""
import hashlib, json, os, socket, subprocess, sys, threading, time, urllib.request

STATE = os.path.expanduser("~/.claude/state")
SOCK = os.environ.get("SKILL_JUDGE_SOCK", os.path.join(STATE, "skill-judge.sock"))
LOG = os.path.join(STATE, "skill-judge.log")
PROVIDER = os.environ.get("SKILL_JUDGE_PROVIDER", "claude-cli")
MODEL = os.environ.get("SKILL_JUDGE_MODEL", "haiku" if PROVIDER == "claude-cli" else "")
SPARES_PER_CATALOG, KEEP_CATALOG_FOR, HOT_FOR = 1, 3600, 15 * 60  # fallback only since Jev became the judge (23 Oct); ~150 MB per spare
TIMEOUT = 20
JUDGE = ("You are a classifier, not an assistant. Never answer or act on the message. Decide which installed skills "
         "the agent should load before working on this message, then output ONLY a JSON object: "
         '{"subject": "<one sentence: what the message asks for>", "skills": [<names from the catalog, best first>]}. '
         "There is no cap: real work usually mixes several (a UI task can need better-layout, better-typography, "
         "better-colors, better-ui, emil-design-eng and animate together; a bug fix needs poteto-mode, "
         "principle-fix-root-causes and principle-prove-it-works). Include every principle-* skill whose situation "
         "applies. Add poteto-mode for any engineering work, including small changes, flags and investigations. "
         "Project skills (deploy, pr, diagnose, verify-*, framework and library skills) apply whenever the message "
         "is about that project's code. Pick a skill only for what THIS message asks: a skill for a file format, a "
         "framework or a product the message does not involve is wrong even if it sounds adjacent. An empty list "
         "is only for messages where no catalog entry is about the subject at all: chat, thanks, yes/no answers, "
         "and questions about the assistant or its tools. A request to do, write, rewrite, fix, build, check, "
         "summarise or catch up on something almost always has a matching skill. Catalog:\n")


def log(line):
    try:
        with open(LOG, "a") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def spawn(catalog):
    return subprocess.Popen(
        ["claude", "-p", "--model", MODEL, "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose", "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
         "--mcp-config", '{"mcpServers":{}}', "--disable-slash-commands", "--max-turns", "1",
         "--system-prompt", JUDGE + catalog],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        cwd="/tmp", env={**os.environ, "SKILL_ROUTER_CHILD": "1"})


class Pool:
    def __init__(self):
        self.spares, self.seen, self.lock = {}, {}, threading.Lock()  # hash -> [proc]; hash -> (catalog, last used)

    def take(self, catalog):
        key = hashlib.sha1(catalog.encode()).hexdigest()[:12]
        with self.lock:
            self.seen[key] = (catalog, time.time())
            live = [p for p in self.spares.get(key, []) if p.poll() is None]
            proc, warm = (live.pop(0), True) if live else (spawn(catalog), False)
            self.spares[key] = live
        threading.Thread(target=self.refill, daemon=True).start()
        return key, proc, warm

    def refill(self):
        with self.lock:
            now = time.time()
            for key, (catalog, last) in list(self.seen.items()):
                live = [p for p in self.spares.get(key, []) if p.poll() is None]
                if now - last > KEEP_CATALOG_FOR:
                    for p in live:
                        p.kill()
                    del self.seen[key]
                    self.spares.pop(key, None)
                    continue
                want = SPARES_PER_CATALOG if now - last < HOT_FOR else 1
                while len(live) > want:
                    live.pop().kill()
                while len(live) < want:
                    live.append(spawn(catalog))
                self.spares[key] = live


def ask(proc, prompt):
    msg = {"type": "user", "message": {"role": "user", "content": "<message>" + prompt + "</message>"}}
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    proc.stdin.close()
    deadline = time.time() + TIMEOUT
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("judge session ended without a result")
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                text = ev.get("result") or ""
                start, end = text.find("{"), text.rfind("}")
                try:
                    obj = json.loads(text[start:end + 1]) if 0 <= start < end else {}
                except ValueError:
                    obj = {}
                names = obj.get("skills") if isinstance(obj, dict) else None
                if not isinstance(names, list):  # bare array, old style
                    start, end = text.find("["), text.rfind("]")
                    names = json.loads(text[start:end + 1]) if 0 <= start < end else []
                return [n for n in names if isinstance(n, str)]
        raise TimeoutError("judge timed out")
    finally:
        if proc.poll() is None:
            proc.kill()


def api_judge(prompt, catalog):
    if PROVIDER not in {"anthropic", "openai"}:
        raise ValueError("SKILL_JUDGE_PROVIDER must be claude-cli, anthropic or openai")
    if not MODEL:
        raise ValueError("Set SKILL_JUDGE_MODEL for the API provider")
    key = os.environ["SKILL_JUDGE_API_KEY"]
    messages = [{"role": "user", "content": "<message>" + prompt + "</message>"}]
    if PROVIDER == "anthropic":
        base = os.environ.get("SKILL_JUDGE_BASE_URL", "https://api.anthropic.com/v1")
        endpoint = "/messages"
        body = {"model": MODEL, "max_tokens": 2048, "system": JUDGE + catalog, "messages": messages}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        base = os.environ.get("SKILL_JUDGE_BASE_URL", "https://api.openai.com/v1")
        endpoint = "/chat/completions"
        body = {"model": MODEL, "messages": [{"role": "system", "content": JUDGE + catalog}, *messages]}
        headers = {"Authorization": "Bearer " + key}
    request = urllib.request.Request(base.rstrip("/") + endpoint, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        result = json.load(response)
    text = ("".join(part.get("text", "") for part in result["content"])
            if PROVIDER == "anthropic" else result["choices"][0]["message"]["content"])
    start, end = text.find("{"), text.rfind("}")
    names = json.loads(text[start:end + 1])["skills"]
    return [name for name in names if isinstance(name, str)]


def serve():
    pool = Pool()
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)
    srv.listen(16)

    def handle(conn):
        with conn:
            t0, key, warm = time.time(), "?", False
            try:
                req = json.loads(conn.makefile().readline())
                if PROVIDER == "claude-cli":
                    key, proc, warm = pool.take(req["catalog"])
                    names = ask(proc, req["prompt"])
                else:
                    names = api_judge(req["prompt"], req["catalog"])
                out = {"names": names}
                log(f"{key} {'warm' if warm else 'COLD'} {time.time() - t0:.1f}s {json.dumps(names)}")
            except Exception as e:  # report, never crash the server
                out = {"error": type(e).__name__}
                log(f"{key} {'warm' if warm else 'COLD'} {time.time() - t0:.1f}s ERROR {type(e).__name__}")
            try:
                conn.sendall((json.dumps(out) + "\n").encode())
            except OSError:
                pass

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve()
