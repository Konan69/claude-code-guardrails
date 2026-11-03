#!/usr/bin/env python3
"""UserPromptSubmit hook that names installed skills matching the prompt."""
import json, math, os, re, socket, sys, time, urllib.request

HOME = os.path.expanduser("~")
USER_SKILLS = os.path.join(HOME, ".claude/skills")
PLUGIN_CACHE = os.path.join(HOME, ".claude/plugins/cache")
SETTINGS = os.path.join(HOME, ".claude/settings.json")
CACHE = os.path.join(HOME, ".claude/state/skill-router-index.json")
TOP = 3
MIN_SCORE = 5.0
STRONG = 10.0  # keyword score that is added on top of the judge; 2+ shared rare words, or a quoted phrase plus one
MAX_WORDS = 2000  # pasted logs and briefs route too; only the keyword fallback stops at 150 words
JUDGE_SOCK = os.environ.get("SKILL_JUDGE_SOCK") or os.path.join(HOME, ".claude/state/skill-judge.sock")
JUDGE = os.environ.get("SKILL_JUDGE", "jev")  # jev | haiku | union
JEV_MODEL = os.environ.get("SKILL_JEV_MODEL", "typesafe/jev-1.14-20261014")  # what the jev CLI resolves on OpenRouter, 23 Oct
JEV_T = float(os.environ.get("SKILL_JEV_T", "0.5"))  # eval 23 Oct: 1.14 at 0.5 = 100% must-have recall, 3/54 false positives; 0.6 drops to 93%
JUDGE_TIMEOUT = 15  # Jev on OpenRouter hits 9 to 11 s a few times a day; the hook budget is 20 (28 Oct)
LOADED = os.path.join(HOME, ".claude/state/skill-loaded")  # per session, written by skill-gate.py; cleared on compaction
PENDING = os.path.join(HOME, ".claude/state/skill-pending")

# Only the synced claude.ai skills retired on 2026-10-22 (duplicates or broken; synced, so not deletable here).
from policy import CONFIG
NEVER_ROUTE = set(CONFIG["router"]["never_route"])
SKIP_PREFIX = ()  # principles are routed too since 2026-10-22: poteto-mode alone never loaded them
# Extra trigger words for skills whose description is too thin to match.
ALIASES = {
    "unslop": "rewrite sound human ai-written slop prose edit writing tells robotic",
    "animate": "animation animate motion transition hover press micro-interaction",
    "fixing-motion-performance": "janky jank stutter stuttering laggy choppy dropped frames",
    "tdd": "failing test regression test tests before changing",
    "better-layout": "hierarchy spacing flat cards grouping alignment cramped",
    "xlsx": "\"spreadsheet\" excel workbook csv",
}
# Question shapes that name a skill outright.
PATTERNS = [
    (re.compile(r"\bhow (does|is|are) (the |our |this |that )?[\w./-]+(\s[\w./-]+){0,4} (work|run|handle|load|get)s?\b|\bwhere should\b.*\blive\b|\bwalk me through\b|\bwhich (package|module|layer) owns\b", re.I), "how"),
    (re.compile(r"\bwhy (did|do|does) (we|you|they) (pick|choose|chose|use|go with|build|do)\b|\bwhy (is|does) (it|this|the [\w-]+) (built|designed|work) (this|that) way\b", re.I), "why"),
    (re.compile(r"\bwhat time is it\b|\bwhat'?s the time\b|\btoday'?s date\b|\bwhat day is (it|today)\b", re.I), "now"),
]
ENGINEERING = re.compile(
    r"\b(fix\w*|bug\w*|implement\w*|refactor\w*|feature|ship|merge|deploy\w*|tests?|debug\w*|crash\w*|"
    r"regression|migrat\w*|perf|performance|endpoint|schema|typecheck|lint)\b", re.I)
STOP = set("""a an the and or of to in on for with by from at as is are be this that it its use used using
when what how why who your you me my we our can do does not no any all into over via than then also
like just only more most such each other skill skills user users asks ask asked request requests
covers cover make makes one two get set run runs without about please want need help write
check move moving still think know look see thing things way something everything away should would could""".split())


def stem(w):
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("y", "")):
        if len(w) >= len(suf) + 4 and w.endswith(suf):
            w = w[: -len(suf)] + rep
            break
    return w[:6]


def tokens(text):
    return [stem(w) for w in re.findall(r"[a-z][a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2]


def frontmatter(path):
    try:
        s = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    if not s.startswith("---") or s.count("---") < 2:
        return None
    head = s.split("---", 2)[1]
    fm = {}
    for key in ("name", "description", "disable-model-invocation"):
        m = re.search(rf"^{key}:\s*(.+?)(?=^\S[\w-]*:|\Z)", head, re.M | re.S)
        if m:
            v = " ".join(m.group(1).split())
            if v[:1] in "'\"" and v[-1:] == v[:1]:
                v = v[1:-1]
            fm[key] = v.replace("\\\"", "\"")
    return fm


def enabled_plugins():
    try:
        return [k for k, v in json.load(open(SETTINGS)).get("enabledPlugins", {}).items() if v]
    except (OSError, ValueError, AttributeError):
        return []


def sources(cwd):
    """(SKILL.md path, invocation prefix) for every skill the Skill tool can load."""
    out = []
    roots = [USER_SKILLS] + ([os.path.join(cwd, ".claude/skills")] if cwd else [])
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root)):
            if d.startswith("."):
                continue
            p = os.path.join(root, d)
            if d == "synced" and root == USER_SKILLS and os.path.isdir(p):
                for sub in sorted(os.listdir(p)):
                    if sub.startswith(".") or not os.path.isdir(os.path.join(p, sub)):
                        continue
                    for s in sorted(os.listdir(os.path.join(p, sub))):
                        out.append((os.path.join(p, sub, s, "SKILL.md"), "anthropic-skills:"))
            else:
                out.append((os.path.join(p, "SKILL.md"), ""))
    for key in enabled_plugins():
        plugin, _, market = key.partition("@")
        base = os.path.join(PLUGIN_CACHE, market, plugin)
        if not os.path.isdir(base):
            continue
        versions = sorted(os.listdir(base))
        skills_dir = os.path.join(base, versions[-1], "skills") if versions else ""
        if os.path.isdir(skills_dir):
            for s in sorted(os.listdir(skills_dir)):
                out.append((os.path.join(skills_dir, s, "SKILL.md"), plugin + ":"))
    return [(f, pre) for f, pre in out if os.path.isfile(f)]


def invoke_only(skill_md, fm):
    """A skill is never routed, suggested or gated when its SKILL.md says
    `disable-model-invocation: true` (Claude) or its agents/openai.yaml says `allow_implicit_invocation: false`
    (Codex). Either flag counts; the skill runs only when the operator types /name or $name."""
    if (fm.get("disable-model-invocation") or "").lower() == "true":
        return True
    try:
        y = open(os.path.join(os.path.dirname(skill_md), "agents", "openai.yaml"), encoding="utf-8").read()
    except OSError:
        return False
    return re.search(r"^\s*allow_implicit_invocation:\s*false\b", y, re.M) is not None


def policy_mtime(skill_md):
    try:
        return os.path.getmtime(os.path.join(os.path.dirname(skill_md), "agents", "openai.yaml"))
    except OSError:
        return 0


def build_index(cwd):
    sig = [[f, pre, os.path.getmtime(f), policy_mtime(f)] for f, pre in sources(cwd)]
    try:
        cached = json.load(open(CACHE))
        if cached.get("v") == 4 and cached.get("sig") == sig and isinstance(cached.get("skills"), list) and isinstance(cached.get("idf"), dict):
            return cached["skills"], cached["idf"]
    except (OSError, ValueError, AttributeError):
        pass
    skills, seen = [], set()
    for f, pre, _, _ in sig:
        fm = frontmatter(f)
        if not fm or not fm.get("name") or invoke_only(f, fm):
            continue
        base = fm["name"]
        if base in NEVER_ROUTE or base.startswith(SKIP_PREFIX):
            continue
        name = pre + base
        if name in seen:
            continue
        seen.add(name)
        desc = fm.get("description", "") + " " + ALIASES.get(base, "")
        phrases = [q.lower() for q in re.findall(r"[\"“]([^\"”]{3,40})[\"”]", desc)]
        skills.append({"name": name, "base": base, "path": f, "desc": fm.get("description", "")[:240], "toks": sorted(set(tokens(desc))), "phrases": phrases})
    df = {}
    for s in skills:
        for t in s["toks"]:
            df[t] = df.get(t, 0) + 1
    n = max(len(skills), 1)
    idf = {t: math.log(n / c) for t, c in df.items()}
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = f"{CACHE}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"v": 4, "sig": sig, "skills": skills, "idf": idf}, fh)
        os.replace(tmp, CACHE)
    except OSError:
        pass
    return skills, idf


def score(prompt, skills, idf, floor=None):
    floor = MIN_SCORE if floor is None else floor
    ptoks = set(tokens(prompt))
    low = prompt.lower()
    norm = max(1.0, math.sqrt(len(ptoks) / 8))  # a longer prompt must match proportionally more
    out = {}
    for s in skills:
        hits = ptoks & set(s["toks"])
        # one shared word is coincidence (22 Oct: "moving" routed a tsgo question to morphing-icons)
        sc = sum(idf.get(t, 0) for t in hits) / norm if len(hits) >= 2 else 0.0
        if "-" in s["base"] and re.search(rf"\b{re.escape(s['base'])}\b", low):
            sc += 10
        sc += sum(4 for p in s["phrases"] if p in low)
        if sc >= floor:
            out[s["name"]] = sc
    for rx, base in PATTERNS:
        if rx.search(prompt):
            for s in skills:
                if s["base"] == base:
                    out[s["name"]] = out.get(s["name"], 0) + 20
    return [n for n, _ in sorted(out.items(), key=lambda kv: -kv[1])[:TOP]]


def jev_key():
    return os.environ.get("OPENROUTER_API_KEY")


def recent_turns(path, prompt, chars=1500):
    """The last assistant reply and the user prompt before it, from the session transcript, so Jev judges a terse
    follow-up in context. Returns "" when there is no transcript."""
    if not path or not os.path.exists(path):
        return ""
    users, assistants = [], []
    try:
        with open(path, errors="ignore") as fh:
            for line in fh.readlines()[-400:]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") not in ("user", "assistant"):
                    continue
                c = (r.get("message") or {}).get("content")
                text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict)
                                                             and b.get("type") == "text")
                text = text.strip()
                if not text or text.startswith(("<system-reminder>", "Base directory for this skill", "<task-notification>",
                                                "[SYSTEM NOTIFICATION", "Delegated task ")):
                    continue
                (users if r["type"] == "user" else assistants).append(text)
    except OSError:
        return ""
    prev_user = next((u for u in reversed(users) if u != prompt), "")
    last_asst = assistants[-1] if assistants else ""
    out = []
    if prev_user:
        out.append("earlier user message: " + prev_user[:chars // 3])
    if last_asst:
        out.append("assistant's last reply: " + last_asst[-chars:])
    return "\n".join(out)


ENGINEERING = 1.0  # Jev's P(message asks for engineering work); gates the verify-* add. Haiku fallback: assume yes.
ROUTER_LOG = os.path.expanduser("~/.claude/state/skill-router.log")


def jev(prompt, skills, context=""):
    """TypeSafe Jev (Jev via OpenRouter): one call, one yes/no question per skill, calibrated
    probabilities. Returns {name: p} or None on any failure. ~1.7 s for 166 skills (measured 2026-10-23)."""
    key = jev_key()
    if not key:
        return None
    qs = {f"s{i}": {"type": "noul", "instructions": f"The skill '{s['name']}' applies to the work the latest message "
                     f"asks for, read with the conversation excerpt. The skill: {s.get('desc', '')}",
                     "criteria": {"true": "the task involves the skill's subject: it will be done with the product, "
                                          "platform, library or format the skill names, or the skill's practice is a "
                                          "direct part of the task",
                                  "false": "the task uses a different product, platform or library for that purpose, or "
                                           "the skill's subject is only adjacent and does not come up in what is asked"}}
          for i, s in enumerate(skills)}
    qs["engineering"] = {"type": "noul", "instructions": "The message asks for engineering work on software: a fix, a "
                         "feature, a flag, a refactor, a migration, a deploy, a test, a check of code or a system, or an "
                         "investigation of why something behaves as it does.",
                         "criteria": {"true": "work on code, config, infrastructure or a running system is requested",
                                      "false": "chat, a question about the assistant, writing prose, design critique "
                                               "without code, or a request outside software"}}
    qs["wants_work"] = {"type": "noul", "instructions": "The message asks the assistant to do, make, change, check, "
                        "explain, find, write or look at something, so loading skills (bodies of instructions for a "
                        "kind of task) before answering could help.",
                        "criteria": {"true": "a request, instruction, question, task, complaint, bug report, or a "
                                             "description of something wrong or wanted in the product or the work, "
                                             "however short or indirect ('ship it', 'the page looks flat', 'still slow')",
                                     "false": "an acknowledgement, approval, thanks, reaction, greeting, yes/no or "
                                              "one-word reply to a previous question, a remark about the assistant itself, or a "
                                              "question about what the assistant did, said, changed or decided in this "
                                              "conversation ('ok', 'yeah okay', 'thanks', 'go ahead', 'did you change "
                                              "more than I asked?', 'why did you do that?')"}}
    state = {"latest_message": prompt}
    if context:
        state["conversation_excerpt"] = context
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": qs}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/alpha/decisions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        out = json.load(urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT))
        if float(out["answers"].get("wants_work", {}).get("noul", 1)) < 0.5:
            return {}  # Jev says this message asks for nothing ; no injection
        probs = {skills[int(k[1:])]["name"]: float(v["noul"]) for k, v in out["answers"].items()
                 if k not in ("engineering", "wants_work")}
        global ENGINEERING
        ENGINEERING = eng = float(out["answers"].get("engineering", {}).get("noul", 0))
        if eng >= 0.5 and any(s["name"] == "poteto-mode" for s in skills):  # CLAUDE.md: every engineering task starts there
            probs["poteto-mode"] = max(probs.get("poteto-mode", 0), eng)
        return probs
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def pick(prompt, skills, context=""):
    """Names, best first, or None when every judge is down."""
    probs = jev(prompt, skills, context) if JUDGE in ("jev", "union") else None
    names = [n for n, p in sorted(probs.items(), key=lambda kv: -kv[1]) if p >= JEV_T] if probs is not None else None
    if JUDGE == "haiku" or (JUDGE == "union" and names is not None) or (JUDGE == "jev" and names is None):
        hk = judge(prompt, skills)
        if hk is not None:
            names = list(dict.fromkeys((names or []) + hk)) if JUDGE == "union" else (names if names is not None else hk)
    return names


def judge(prompt, skills):
    """Warm Haiku via skill-judge-daemon.py . None on any failure."""
    catalog = "\n".join(f"- {s['name']}: {s.get('desc', '')}" for s in skills)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(JUDGE_TIMEOUT)
            c.connect(JUDGE_SOCK)
            c.sendall((json.dumps({"prompt": prompt, "catalog": catalog}) + "\n").encode())
            out = json.loads(c.makefile().readline())
    except (OSError, ValueError):
        return None
    if not isinstance(out.get("names"), list):
        return None
    names = {s["name"] for s in skills}
    return list(dict.fromkeys(n for n in out["names"] if n in names))


def main():
    if os.environ.get("SKILL_ROUTER_CHILD"):
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    prompt = (data.get("prompt") or "").strip()
    words = len(prompt.split())
    if not prompt or prompt.startswith("/") or words < 3 or words > MAX_WORDS:
        return
    if prompt.startswith(("<system-reminder>", "[SYSTEM NOTIFICATION", "<task-notification>", "Delegated task ")):
        return  # background-task events and T3 child wake-ups; delegated children and agent briefs do real work
    skills, idf = build_index(data.get("cwd") or "")
    t0 = time.time()
    context = recent_turns(data.get("transcript_path"), prompt)
    names = pick(prompt, skills, context)  # [] is a real answer: nothing applies
    try:
        with open(ROUTER_LOG, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {JUDGE} {time.time() - t0:.1f}s eng={ENGINEERING:.2f} "
                     f"{json.dumps(prompt[:80])} -> {json.dumps(names)}\n")
    except OSError:
        pass
    if names is None:  # every judge down: say so instead of guessing
        print("skill router: the skill judges (Jev, Haiku) are unreachable, so no skills were matched for this "
              "message. Pick the relevant installed skills yourself from the Skill tool's list and load them first.")
        return
    if names and ENGINEERING >= 0.5:  # CLAUDE.md: the repo's verify-<app> skill is mandatory for any change there
        for s in skills:
            if s["base"].startswith("verify-") and s["name"] not in names and not s["path"].startswith(USER_SKILLS):
                names.append(s["name"])
    for rx, base in PATTERNS:  # question shapes that name a skill outright
        hit = next((s["name"] for s in skills if s["base"] == base), None)
        if hit and rx.search(prompt) and hit not in names:
            names.insert(0, hit)
    if not names:
        return
    sid = re.sub(r"[^\w-]", "", str(data.get("session_id") or ""))
    try:  # skill-gate.py records every Skill call; don't ask again for what this session already loaded
        loaded = set(json.load(open(os.path.join(LOADED, sid)))) if sid else set()
    except (OSError, ValueError):
        loaded = set()
    todo = [n for n in names if n not in loaded]
    if not todo:
        return
    if sid:  # skill-gate.py holds edits and commands until all of these are loaded
        try:
            os.makedirs(PENDING, exist_ok=True)
            with open(os.path.join(PENDING, sid), "w") as fh:
                json.dump({"names": todo, "at": time.time()}, fh)
        except OSError:
            pass
    by_name = {s["name"]: s for s in skills}
    lines = "\n".join(f"- {n}: {by_name[n].get('desc', '')[:160]}" for n in todo if n in by_name)
    text = (MESSAGE.format(names=", ".join(todo)) + "\n" + lines)[:9500]  # hook text over ~10 KB becomes a file
    if os.environ.get("SKILL_ROUTER_ASYNC"):  # async hook : the output reaches the
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",  # model as an attachment
                                                 "additionalContext": text}}))        # on its next turn
        return
    print(text)


# Full skill text can't ride in the hook: Claude Code swaps hook output over ~10 KB for a 2 KB preview and a file
# path (measured 2026-10-23; nine picked skills came to 121 KB). The Skill tool has no such cap.
MESSAGE = ("skill router: Jev matched these installed skills to this message: {names}. Load each that applies with "
           "the Skill tool before any other tool call (all in one turn) and follow them together; edits and commands "
           "are held until then. One that is plainly off-subject can stay unloaded; do not explain or mention it.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block a prompt
        print(f"skill router failed open: {e}", file=sys.stderr)
