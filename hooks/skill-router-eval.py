#!/usr/bin/env python3
"""Score the router against labelled prompts and the locally installed skill catalog.

Each case names a prompt, working directory, required skills and forbidden skills.
Evaluation needs a matching local skill catalog and a configured judge.
"""
import json, os, subprocess, sys, time

HOME = os.path.expanduser("~")
ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill-router.py")
PROJECT_A, PROJECT_B = os.path.join(HOME, "work/PROJECT_A"), os.path.join(HOME, "work/project-b")
CASES = [
    # prompt, cwd, must, must_not
    ("the settings page looks flat and the spacing is off, make it feel premium", PROJECT_A,
     {"better-layout", "better-ui", "poteto-mode", "verify-project-a"}, {"anthropic-skills:docx", "anthropic-skills:pptx"}),
    ("the empty state text on the inbox is confusing, rewrite the labels and button copy", PROJECT_A,
     {"better-writing", "verify-project-a"}, {"unslop", "anthropic-skills:docx"}),
    ("the sheet drag feels wrong, it snaps instead of following my finger", PROJECT_B,
     {"apple-design", "animate"}, {"anthropic-skills:pptx", "product-film"}),
    ("add a bottom sheet with a spring and haptics to the expo app", PROJECT_B,
     {"animate-expo"}, {"mobile-native", "anthropic-skills:docx"}),
    ("the web app feels like a website on my phone, inputs zoom and hover sticks", PROJECT_A,
     {"mobile-native"}, {"animate-expo"}),
    ("the login test is flaky, find out why and fix it", PROJECT_A,
     {"poteto-mode", "principle-fix-root-causes", "verify-project-a"}, {"better-layout", "animate"}),
    ("the dashboard query takes 4s, it was 200ms last week, measured with the bench script", PROJECT_A,
     {"benchmark-checklist", "principle-explain-the-number"}, {"better-ui"}),
    ("refactor the session store into one module and delete the old helpers", PROJECT_A,
     {"poteto-mode", "principle-laziness-protocol"}, {"better-ui", "animate"}),
    ("wrap the stripe client in an Effect service with typed errors and a layer", PROJECT_A,
     {"effect"}, {"better-ui", "anthropic-skills:xlsx"}),
    ("write a helix query that finds bookmarks mentioning two entities with BM25", HOME,
     {"helix-query-typescript"}, {"helix-cli", "better-ui"}),
    ("add a durable object that holds the room state and a queue consumer for events", PROJECT_A,
     {"cloudflare:durable-objects"}, {"better-ui", "animate"}),
    ("switch the chat route to streamText with tool calling and useChat on the client", PROJECT_A,
     {"ai-sdk"}, {"anthropic-skills:docx"}),
    ("log into upwork on cloudbox and screenshot the proposals page", HOME,
     {"pi-browser"}, {"poteto-mode", "better-ui"}),
    ("generate a hero image for the example campaign, pink and playful", HOME,
     {"example-brand", "image-gen"}, {"better-colors"}),
    ("clean up this messy spreadsheet csv for me please", HOME,
     {"anthropic-skills:xlsx"}, {"anthropic-skills:docx", "poteto-mode"}),
    ("write the README and a PR description for the new verify skill", PROJECT_A,
     {"technical-writing"}, {"anthropic-skills:docx", "better-writing"}),
    ("rewrite this paragraph so it doesnt sound ai-written", HOME,
     {"unslop"}, {"better-writing", "technical-writing"}),
    ("how does the chat session get loaded in project-b", PROJECT_B, {"how"}, {"why"}),
    ("why did we pick helix over neo4j for the kb", HOME, {"why"}, {"how"}),
    ("catch me up on what we did on project-b this week", PROJECT_B, {"recall"}, {"poteto-mode", "verify-project-b"}),
    ("daily update for teammate", HOME, {"daily-update"}, {"poteto-mode"}),
    ("a lil buggy. r u using haiku?", HOME, set(), {"poteto-mode", "tdd"}),
    ("thanks that works", HOME, set(), {"poteto-mode"}),
    ("yes do it", HOME, set(), {"poteto-mode"}),
    ("Yeah okay", HOME, set(), {"ketchup", "poteto-mode"}),  # 23 Oct: got ketchup
    ("yeah okay do it then", PROJECT_A, set(), {"ketchup", "poteto-mode", "verify-project-a"}),
    ("ok go ahead with that", PROJECT_A, set(), {"ketchup", "poteto-mode", "verify-project-a"}),
    ("nice, looks good", PROJECT_B, set(), {"ketchup", "better-ui", "verify-project-b"}),
    ("lol ok", HOME, set(), {"ketchup"}),
    ("catch me up on what changed in this chat since i left", PROJECT_A, {"ketchup"}, {"verify-project-a", "poteto-mode"}),
    ("which skills do you have loaded right now", HOME, set(), {"poteto-mode", "find-skills"}),
    ("hand the backend wiring for the rpc layer to sol in parallel lanes", PROJECT_A,
     {"codex-lanes"}, {"better-ui"}),
    ("review this PR for correctness and tear it apart", PROJECT_A, {"interrogate"}, {"better-ui"}),
    ("make a 20s launch film for project-b from the real app components", PROJECT_B,
     {"product-film"}, {"no-slop-motion"}),
    ("blast radius of changing the session id type from string to branded", PROJECT_A,
     {"blast-radius"}, {"better-ui"}),
    ("Can you pause scheduled automations on preview? Should be a flag", PROJECT_A,
     {"poteto-mode", "verify-project-a"}, {"anthropic-skills:docx", "anthropic-skills:pptx", "vercel-react-best-practices"}),
    ("Please check what is still using TS Go and I think OX Lens is still using a TS Go type of lens so we should "
     "be moving everything away from the TS Go. I don't know why OX Lens is still slow", PROJECT_A,
     {"poteto-mode"}, {"morphing-icons", "anthropic-skills:docx"}),
    ("the drawer animation is janky on mobile, can you look", PROJECT_B,
     {"fixing-motion-performance"}, {"anthropic-skills:pptx"}),
    ("Build the onboarding screen: three steps, progress dots, a skip link, and it has to feel native on iOS", PROJECT_B,
     {"animate-expo", "better-layout", "poteto-mode"}, {"mobile-native", "anthropic-skills:docx"}),
    ("the kb search returns nothing for 'helix' even though 40 bookmarks mention it", HOME,
     {"bookmark-refresh", "principle-fix-root-causes"}, {"better-ui"}),
    ("deploy the worker and check the logs after", PROJECT_A, {"cloudflare:wrangler"}, {"better-ui"}),
    # invoke-only skills (disable-model-invocation / allow_implicit_invocation: false) are never routed
    ("be ambitious, raise the bar on this task: turn my request into the deeper version a top professional would do",
     PROJECT_A, set(), {"be-ambitious"}),
    ("use be-ambitious on my last request", HOME, set(), {"be-ambitious"}),
]


def run_case(prompt, cwd, backend):
    env = dict(os.environ)
    judge, _, t = backend.partition("@")  # e.g. jev@0.6, haiku, union@0.7
    env["SKILL_JUDGE"] = judge
    if t:
        env["SKILL_JEV_T"] = t
    t = time.time()
    o = subprocess.run(["python3", ROUTER], input=json.dumps({"prompt": prompt, "cwd": cwd, "session_id": "eval"}),
                       capture_output=True, text=True, env=env)
    dt = time.time() - t
    pend = os.path.join(HOME, ".claude/state/skill-pending/eval")
    if os.path.exists(pend):
        os.unlink(pend)
    head = o.stdout.split("message: ")[1].split("\n")[0] if "message: " in o.stdout else ""
    names = head.split(". Load")[0].split(", ")
    return [n for n in names if n], dt


def main():
    backend = sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else "jev"
    must_hit = must_total = fp = fp_total = 0
    lat, lines = [], []
    for prompt, cwd, must, must_not in CASES:
        got, dt = run_case(prompt, cwd, backend)
        lat.append(dt)
        gs = set(got)
        missed, wrong = must - gs, must_not & gs
        must_hit += len(must & gs)
        must_total += len(must)
        fp += len(wrong)
        fp_total += len(must_not)
        flag = "ok " if not missed and not wrong else "BAD"
        lines.append(f"{flag} {dt:4.1f}s {prompt[:48]:48} missed={sorted(missed)} wrong={sorted(wrong)} n={len(got)}")
    print("\n".join(lines))
    lat.sort()
    print(f"\nbackend={backend} must-have recall {must_hit}/{must_total} = {must_hit / must_total:.0%}; "
          f"must-not false positives {fp}/{fp_total}; latency p50 {lat[len(lat) // 2]:.1f}s p90 "
          f"{lat[int(len(lat) * .9)]:.1f}s max {lat[-1]:.1f}s")


if __name__ == "__main__":
    main()
