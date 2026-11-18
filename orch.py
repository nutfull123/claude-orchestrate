#!/usr/bin/env python3
"""Task-graph board for the orchestrate skill.

  orch.py serve [port]              serve the dashboard for ./.orchestra/plan.json
  orch.py set <id> <status> [note]  update one task (pending|running|done|failed|skipped)
  orch.py check                     validate plan.json (ids unique, deps exist, no cycles)
  orch.py board                     print the task table (planned vs actual model/effort) in the terminal
  orch.py watch [folder]            live block panel in a side pane (default: current folder)
  orch.py statusline [-- <cmd...>]  status line: runs <cmd> (your own status line) then adds orchestra progress
  orch.py briefs                    chat-ready Markdown: split table, shared blocks once, every worker prompt, approval question
  orch.py approve [--auto <why>]    record approval and freeze the prompts; dispatch is blocked until then
  orch.py hook                      Claude Code hook: reads the event JSON on stdin

The hook marks a task `running` when an Agent call's description starts with its id
(e.g. "t2 build slugify") and `review` when that subagent finishes, recording the model and
effort that actually ran (from the subagent transcript) and printing a line in the terminal.
Only the lead sets done/failed, after judging the task's check.
"""
import json, os, re, sys, fcntl, tempfile, http.server, socketserver
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLAN = os.path.join(os.getcwd(), ".orchestra", "plan.json")
STATUSES = {"pending", "running", "review", "done", "failed", "skipped"}
MODELS = {"haiku", "sonnet", "opus", "fable"}  # aliases the Agent tool's per-call `model` accepts
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    with open(PLAN, encoding="utf-8") as f:
        return json.load(f)


def save(plan):
    plan["updated"] = now()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PLAN), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAN)  # atomic, so the dashboard never reads half a file


def check(plan):
    tasks = plan.get("tasks", [])
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task id"
    known = set(ids)
    deps = {t["id"]: t.get("deps", []) for t in tasks}
    for tid, ds in deps.items():
        for d in ds:
            assert d in known, f"{tid} depends on unknown task {d}"
    for t in tasks:
        assert t.get("status", "pending") in STATUSES, f"{t['id']}: bad status {t.get('status')}"
        if t.get("agent") != "lead":
            assert t.get("model") in MODELS, f"{t['id']}: model must be one of {sorted(MODELS)}"
            assert t.get("effort") in EFFORTS, f"{t['id']}: effort must be one of {sorted(EFFORTS)}"
            assert t.get("why"), f"{t['id']}: add a short 'why' for the model choice"
    state = {}  # cycle check by DFS

    def visit(n):
        if state.get(n) == 1:
            raise AssertionError(f"dependency cycle at {n}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps[n]:
            visit(d)
        state[n] = 2

    for n in deps:
        visit(n)


@contextmanager
def locked():
    # parallel hooks and the lead write plan.json at the same time; serialize read-modify-write
    with open(os.path.join(os.path.dirname(PLAN), ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def apply(t, status, note=None):
    t["status"] = status
    if status == "running":
        t.setdefault("started", now())
    if status == "review":
        t["returned"] = now()
    if status in ("done", "failed", "skipped"):
        t["finished"] = now()
    if note is not None:
        t["note"] = note


def cmd_set(tid, status, note=None):
    if status not in STATUSES:
        sys.exit(f"status must be one of {sorted(STATUSES)}")
    with locked():
        plan = load()
        for t in plan["tasks"]:
            if t["id"] == tid:
                apply(t, status, note)
                save(plan)
                print(f"{tid} -> {status}")
                return
    sys.exit(f"no task {tid}")


def profile(agent):
    """Planned (model, effort) for an agent: subagents from ~/.claude/agents/<name>.md, lead from settings."""
    home = os.path.expanduser("~/.claude")
    try:
        if agent == "lead":
            s = json.load(open(os.path.join(home, "settings.json")))
            model = s.get("model", "?")
            return model, s.get("modelSettings", {}).get(model, {}).get("effortLevel", "?")
        front = open(os.path.join(home, "agents", f"{agent}.md"), encoding="utf-8").read().split("---")[1]
        kv = dict(re.findall(r"^(\w+):\s*(\S+)\s*$", front, re.M))
        return kv.get("model", "inherit"), kv.get("effort", "default")
    except (OSError, IndexError, ValueError):
        return "?", "?"


def usage(transcript):
    """What actually ran, read from the subagent's own transcript."""
    models, efforts, turns, out, tools = {}, set(), 0, 0, 0
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") != "assistant":
                continue
            m = d.get("message", {})
            models[m.get("model")] = models.get(m.get("model"), 0) + 1
            efforts.add(str(d.get("effort")))
            turns += 1
            out += (m.get("usage") or {}).get("output_tokens", 0)
            tools += sum(1 for c in m.get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use")
    model = max(models, key=models.get) if models else "?"
    return {"model": model, "effort": ",".join(sorted(efforts)) or "?", "turns": turns, "out_tokens": out, "tool_uses": tools}


def short(model):
    return str(model).removeprefix("claude-")


def task_id(desc, tasks):
    m = re.match(r"\s*([\w.-]+?)[:\s]", (desc or "") + " ")
    return m.group(1) if m and m.group(1) in tasks else None


class Deny(str):
    """A hook result that blocks the tool call instead of printing a message."""


def deny(reason):
    return Deny(reason)


def brief_path(tid):
    return os.path.join(os.path.dirname(PLAN), "briefs", f"{tid}.md")


def read_brief(tid, suffix=".md"):
    try:
        return open(brief_path(tid).removesuffix(".md") + suffix, encoding="utf-8").read().strip()
    except OSError:
        return ""


def read_th(tid):
    """Plain-Thai explanation of a task's prompt, written for the user (.orchestra/briefs/<id>.th.md)."""
    return read_brief(tid, ".th.md")


def shared_blocks(briefs):
    """XML-style blocks (<contract>…</contract>) that appear word for word in 2+ prompts: shown once, not repeated."""
    seen = {}
    for b in briefs.values():
        for m in re.finditer(r"<(\w+)>.*?</\1>", b, re.S):
            seen.setdefault(m.group(0), set()).add(id(b))
    return [blk for blk, owners in seen.items() if len(owners) > 1]


def cmd_briefs():
    """Print the plan and every worker prompt as chat-ready Markdown, for the user to approve before dispatch.

    Blocks shared by several prompts are shown once at the top and replaced in each prompt by a marker,
    so the user reads each prompt's own part without scrolling past the same contract five times.
    """
    plan = load()
    tasks = plan.get("tasks", [])
    briefs = {t["id"]: read_brief(t["id"]) for t in tasks if t.get("agent") != "lead"}
    missing = [tid for tid, b in briefs.items() if not b]
    shared = shared_blocks({k: v for k, v in briefs.items() if v})
    names = {blk: re.match(r"<(\w+)>", blk).group(1) for blk in shared}
    lanes = [w for w in waves(tasks)]
    out = [f"## แผนการแบ่งงาน: {plan.get('goal', '')}", "",
           "| งาน | ใครทำ | โมเดล | รอ | เหตุผลที่เลือกโมเดล |", "|---|---|---|---|---|"]
    for t in tasks:
        who = "lead ทำเอง" if t.get("agent") == "lead" else t.get("agent", "?")
        model = "Opus (lead)" if t.get("agent") == "lead" else f"{t.get('model')} · {t.get('effort')}"
        out.append(f"| {t['id']} {t.get('title', '')} | {who} | {model} | {', '.join(t.get('deps', [])) or '-'} | {t.get('why', '-')} |")
    out += ["", "ลำดับการส่ง: " + " → ".join(
        ("[" + " + ".join(x["id"] for x in w) + "] พร้อมกัน") if len(w) > 1 else w[0]["id"] for w in lanes)]
    if shared:
        out += ["", "### ส่วนที่ใช้ร่วมกันในหลาย prompt (แสดงครั้งเดียว)"]
        th_shared = read_th("shared")
        if th_shared:
            out += ["", "**อธิบายภาษาไทย:**", "", th_shared]
        for blk in shared:
            users = [tid for tid, b in briefs.items() if blk in b]
            out += ["", f"`<{names[blk]}>` — อยู่ใน {', '.join(users)}", "```", blk, "```"]
    for t in tasks:
        if t.get("agent") == "lead":
            out += ["", f"### {t['id']} · lead ทำเอง — {t.get('title', '')} (ไม่มี prompt)"]
            continue
        b = briefs[t["id"]]
        for blk in shared:
            b = b.replace(blk, f"<{names[blk]}> …ส่วนกลางด้านบน… </{names[blk]}>")
        th = read_th(t["id"])
        out += ["", f"### {t['id']} → {t.get('agent')} · {t.get('model')} {t.get('effort')} — {t.get('title', '')}",
                "", "**ภาษาไทย (เข้าใจง่าย):**", "", th or "(ยังไม่มีคำอธิบายภาษาไทย)",
                "", "**prompt จริงที่จะส่ง:**", "```", b or f"(ยังไม่มี — เขียน {brief_path(t['id'])})", "```"]
        if not th:
            missing.append(t["id"] + ".th")
    out += ["", "**อนุมัติไหม?**", "1. **อนุมัติ** — ส่งงานรอบแรกออกไปตามแผนนี้",
            "2. **แก้…** — บอกงานและสิ่งที่จะแก้ เช่น `t2 ใช้ opus` หรือ `prompt t3 เพิ่ม …` แล้วจะส่งให้ดูใหม่"]
    print("\n".join(out))
    if missing:
        sys.exit(f"\nยังไม่มี prompt ของ: {', '.join(missing)}")


def cmd_approve(auto_reason=None):
    """Record the user's approval and freeze each task's prompt. Re-run after any edit to a brief."""
    with locked():
        plan = load()
        try:
            check(plan)
        except AssertionError as e:
            sys.exit(f"invalid plan: {e}")
        missing = []
        for t in plan.get("tasks", []):
            if t.get("agent") == "lead":
                continue
            b = read_brief(t["id"])
            if not b:
                missing.append(t["id"])
            if not read_th(t["id"]):  # the user approves what they can read: the Thai explanation is required
                missing.append(t["id"] + ".th.md")
            t["brief"] = b
            t["brief_th"] = read_th(t["id"])
        if missing:
            sys.exit(f"ยังอนุมัติไม่ได้ — ยังขาดไฟล์ใน .orchestra/briefs/: {', '.join(m if m.endswith('.md') else m + '.md' for m in missing)}")
        plan["approval"] = {"at": now(), "by": "auto" if auto_reason else "user", "note": auto_reason or ""}
        save(plan)
    print("approved" + (f" (auto: {auto_reason})" if auto_reason else " by user"))


def port_alive(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except (OSError, ValueError, TypeError):
        return False


def ensure_dashboard(plan):
    """Start this project's dashboard server once and open it in the browser. Returns its URL.

    Set ORCH_NO_DASHBOARD=1 to skip. Opens the browser only when the server is first started,
    so later dispatches in the same run do not keep stealing focus.
    """
    import socket, subprocess, webbrowser
    if os.environ.get("ORCH_NO_DASHBOARD"):
        return None
    d = plan.get("dashboard") or {}
    if d.get("port") and port_alive(d["port"]):
        return d.get("url")
    with socket.socket() as s:  # let the OS pick a free port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proj = os.path.dirname(os.path.dirname(PLAN))
    log = open(os.path.join(os.path.dirname(PLAN), "dashboard.log"), "a")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", str(port), "--exact"],
                         cwd=proj, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(20):  # wait up to ~2s for it to listen
        if port_alive(port):
            break
        import time
        time.sleep(0.1)
    plan["dashboard"] = {"url": url, "port": port, "pid": p.pid}
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return url


def cmd_hook(ev):
    """Track subagents on the board; return a line to show in the Claude terminal (or None)."""
    global PLAN
    PLAN = os.path.join(ev.get("cwd") or os.getcwd(), ".orchestra", "plan.json")
    if not os.path.exists(PLAN):
        return None
    name = ev.get("hook_event_name")
    with locked():
        plan = load()
        tasks = {t["id"]: t for t in plan.get("tasks", [])}
        if name == "PreToolUse" and ev.get("tool_name") in ("Agent", "Task"):
            ti = ev.get("tool_input") or {}
            tid = task_id(ti.get("description"), tasks)
            if not tid or tasks[tid].get("status") not in ("pending", "failed", None):
                return None
            t = tasks[tid]
            # gate: the user must have approved the plan and this task's prompt before anything is sent
            if not plan.get("approval"):
                return deny(f"{tid}: แผนยังไม่ได้รับอนุมัติ — รัน `orch.py briefs`, แสดงให้ผู้ใช้ดู, รอคำตอบ แล้วรัน `orch.py approve`")
            brief = (t.get("brief") or "").strip()
            if not brief:
                return deny(f"{tid}: ไม่มี prompt ที่อนุมัติไว้ — เขียน .orchestra/briefs/{tid}.md ด้วยสกิล prompt-master แล้วขออนุมัติใหม่")
            if not (ti.get("prompt") or "").strip().startswith(brief):
                return deny(f"{tid}: prompt ที่ส่งไม่ตรงกับที่ผู้ใช้อนุมัติ — ส่งข้อความใน .orchestra/briefs/{tid}.md ตามตัวอักษร "
                            "(ต่อท้ายผลของงานก่อนหน้าได้ แต่ห้ามแก้ส่วนที่อนุมัติ) หรือขออนุมัติใหม่")
            agent = ti.get("subagent_type") or t.get("agent", "?")
            dm, de = profile(agent)
            # the plan's per-task choice is the target; the call's own params are what was actually requested
            t["planned"] = {"agent": agent, "model": t.get("model") or dm, "effort": t.get("effort") or de}
            sent = (ti.get("model") or dm, ti.get("effort") or de)
            t["sent_prompt"] = (ti.get("prompt") or "")[:8000]  # approved brief + any appended dependency results
            if ev.get("transcript_path"):  # subagent transcripts live under <session>/subagents/
                plan["session_dir"] = ev["transcript_path"].removesuffix(".jsonl")
            apply(t, "running")
            url = ensure_dashboard(plan)
            save(plan)
            warn = "" if sent == (t["planned"]["model"], t["planned"]["effort"]) else f"  ⚠ call sent {sent[0]} · {sent[1]}"
            dash = f"  · dashboard {url}" if url else ""
            return f"▶ {tid} → {agent} [{t['planned']['model']} · {t['planned']['effort']}]{warn}  {t.get('title', '')}{dash}"
        if name == "SubagentStop":
            path = ev.get("agent_transcript_path") or ""
            try:  # the meta file next to the transcript holds the Agent call's description
                desc = json.load(open(path.removesuffix(".jsonl") + ".meta.json")).get("description")
            except (OSError, ValueError):
                desc = None
            tid = task_id(desc, tasks)
            if not tid or tasks[tid].get("status") != "running":
                return None
            t = tasks[tid]
            u = usage(path)
            t["actual"] = u
            p = t.get("planned", {})
            ok = str(p.get("model", "")).split("-")[0] in u["model"] and p.get("effort") == u["effort"]
            t["verified"] = ok
            apply(t, "review", f"returned · {u['turns']} turns · {u['tool_uses']} tools · {u['out_tokens']} out tokens")
            save(plan)
            mark = "✓ matches plan" if ok else f"⚠ planned {p.get('model')} · {p.get('effort')}"
            return (f"■ {tid} returned  ran on {short(u['model'])} · effort {u['effort']}  {mark}  "
                    f"({u['turns']} turns, {u['tool_uses']} tools, {u['out_tokens']} out tokens) → awaiting lead check")
    return None


ICON = {"pending": "○ pending", "running": "● running", "review": "◐ review", "done": "✓ done",
        "failed": "✗ failed", "skipped": "– skipped"}


COLOR = {"done": "32", "running": "36", "review": "35", "failed": "31", "skipped": "33"}
SPIN = "▁▃▅▇▅▃"  # running blocks cycle through these, one frame per status line refresh


def frame():
    import time
    return SPIN[int(time.time() * 4) % len(SPIN)]  # 4 fps in watch; the status line samples it once a second


def block(t, width=3):
    """A task as a colored block in the progress bar: solid when done, animated while running."""
    s = t.get("status", "pending")
    ch = {"done": "█", "review": "▓", "failed": "█", "skipped": "▒"}.get(s, "░")
    if s == "running":
        ch = frame()
    return f"\033[{COLOR.get(s, '90')}m{ch * width}\033[0m"


def chip(t):
    """One task as a short colored token for the status line."""
    s = t.get("status", "pending")
    color = COLOR.get(s, "90")
    sym = {"done": "█", "review": "▓", "failed": "✗", "skipped": "▒"}.get(s, "░")
    if s == "running":
        sym = frame()
    a = t.get("actual")
    if a:  # proven from the subagent's transcript
        who = f"{short(a['model']).split('-')[0]}·{a['effort']}" + ("✓" if t.get("verified") else "⚠")
    elif t.get("agent") == "lead":
        who = "lead"
    else:
        who = f"{t.get('model', '?')}·{t.get('effort', '?')}"
    return f"\033[{color}m{sym}{t['id']} {who}\033[0m"


def waves(tasks):
    """Group tasks by dependency depth: everything in one wave can run in parallel."""
    by = {t["id"]: t for t in tasks}
    depth = {}

    def d(tid, seen=()):
        if tid in depth:
            return depth[tid]
        if tid in seen or tid not in by:
            return 0
        depth[tid] = max([d(x, seen + (tid,)) + 1 for x in by[tid].get("deps", [])] or [0])
        return depth[tid]

    out = {}
    for t in tasks:
        out.setdefault(d(t["id"]), []).append(t)
    return [out[k] for k in sorted(out)]


def secs(t):
    """Seconds a task has run (still running) or took (finished)."""
    def ts(k):
        try:
            return datetime.fromisoformat(t[k]).timestamp()
        except (KeyError, ValueError, TypeError):
            return None
    import time
    start, end = ts("started"), ts("finished") or (ts("returned") if t.get("status") == "review" else None)
    if start is None:
        return None
    return int((end or time.time()) - start)


def fmt_s(n):
    return "" if n is None else (f"{n}s" if n < 60 else f"{n // 60}m{n % 60:02d}s")


def row(t, by):
    """One task line: animated block, id, role, model·effort (✓ once proven), state, title → detail."""
    s = t.get("status", "pending")
    a = t.get("actual")
    if a:
        who = f"{short(a['model']).split('-')[0]}·{a['effort']}" + (" ✓" if t.get("verified") else " ⚠")
    elif t.get("agent") == "lead":
        who = "opus·lead"
    else:
        who = f"{t.get('model', '?')}·{t.get('effort', '?')}"
    if s == "pending":
        wait = [x for x in t.get("deps", []) if by.get(x, {}).get("status") != "done"]
        state = f"waits {','.join(wait)}" if wait else "ready"
    else:
        state = {"running": "running", "review": "checking", "done": "done", "failed": "FAILED",
                 "skipped": "skipped"}.get(s, s) + (f" {fmt_s(secs(t))}" if secs(t) is not None else "")
    detail = t.get("note") if s in ("done", "failed", "review", "skipped") and t.get("note") else ""
    text = f"{t['id']:<4} {t.get('agent', '?'):<10} {who:<17} {state:<14} {t.get('title', '')}"
    if detail:
        text += f"  → {detail}"
    c = COLOR.get(s, "90")
    return f"{block(t, 1)} \033[{c if s != 'pending' else '90'}m{text}\033[0m"


def panel(plan, width=110):
    """Block view of the whole run: progress bar, then one section per wave with a row per task."""
    tasks = plan.get("tasks", [])
    by = {t["id"]: t for t in tasks}
    done = sum(t.get("status") in ("done", "skipped") for t in tasks)
    bad = [t["id"] for t in tasks if t.get("actual") and not t.get("verified")]
    proven = sum(1 for t in tasks if t.get("actual"))
    head = f"\033[1m◆ orchestra\033[0m ▕{''.join(block(t) for t in tasks)}▏ {done}/{len(tasks)}"
    head += f" · verified {proven - len(bad)}/{proven}" + (f" \033[31m⚠ {','.join(bad)}\033[0m" if bad else "")
    goal = plan.get("goal", "")
    lines = [head, f"\033[2m  {goal[:width - 2]}\033[0m"] if goal else [head]
    if tasks and done == len(tasks) and not any(t.get("status") == "failed" for t in tasks):
        return lines + ["  " + " ".join(chip(t) for t in tasks) + "  \033[32mfinished\033[0m"]
    for n, wave in enumerate(waves(tasks), 1):
        par = " · parallel" if len(wave) > 1 else ""
        lines.append(f"\033[2m  ── wave {n}{par} " + "─" * 20 + "\033[0m")
        for t in wave:
            lines.append("  " + clip(row(t, by), width - 2))
    return lines


def clip(s, width):
    """Cut a colored line to `width` visible characters, keeping its ANSI codes intact."""
    out, n, i = [], 0, 0
    while i < len(s):
        if s[i] == "\033":
            j = s.index("m", i) + 1
            out.append(s[i:j])
            i = j
            continue
        if n >= width:
            out.append("…\033[0m")
            break
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out)


def orchestra_lines(cwd):
    global PLAN
    PLAN = os.path.join(cwd, ".orchestra", "plan.json")
    if not os.path.exists(PLAN):
        return []
    return panel(load())


def cmd_statusline(base):
    """Print the user's own status line (if any), then the orchestra progress for this folder."""
    import subprocess
    raw = sys.stdin.read()
    if base:
        try:
            out = subprocess.run(base, input=raw, capture_output=True, text=True, timeout=5).stdout.rstrip("\n")
            if out:
                print(out)
        except Exception:
            pass
    try:
        ev = json.loads(raw or "{}")
        cwd = (ev.get("workspace") or {}).get("current_dir") or ev.get("cwd") or os.getcwd()
        for line in orchestra_lines(cwd):
            print(line)
    except Exception:
        pass  # a broken plan must never blank the status line


def cmd_watch():
    """Live block view for a side terminal pane: redraws 4x a second until Ctrl-C."""
    import time, shutil
    try:
        while True:
            width = shutil.get_terminal_size((120, 40)).columns
            body = panel(load(), width) if os.path.exists(PLAN) else [f"waiting for {PLAN}"]
            sys.stdout.write("\033[H\033[2J" + "\n".join(body) + "\n")
            sys.stdout.flush()
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass


def cmd_board():
    if not os.path.exists(PLAN):
        sys.exit(f"no plan yet: {PLAN}")
    plan = load()
    tasks = plan.get("tasks", [])
    done = sum(t.get("status") == "done" for t in tasks)
    print(f"{plan.get('goal', '')}\n{done}/{len(tasks)} done\n")
    rows = [("ID", "STATUS", "AGENT", "PLANNED", "ACTUALLY RAN", "TASK")]
    for t in tasks:
        agent = t.get("agent", "?")
        p = t.get("planned") or ({"model": t["model"], "effort": t.get("effort")} if t.get("model")
                                 else dict(zip(("model", "effort"), profile(agent))))
        a = t.get("actual")
        ran = "-" if not a else f"{short(a['model'])} · {a['effort']} " + ("✓" if t.get("verified") else "⚠")
        rows.append((t["id"], ICON.get(t.get("status", "pending"), t.get("status")), agent,
                     f"{p.get('model')} · {p.get('effort')}", ran, t.get("title", "")))
    w = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)) + "  " + r[5])
    whys = [t for t in tasks if t.get("why")]
    if whys:
        print("\nWhy this model:")
        for t in whys:
            print(f"  {t['id']}  {t.get('model')} · {t.get('effort')}  {t['why']}")


def summarize_tool(name, inp):
    """One readable line for a tool call: what the agent is doing, not its raw JSON."""
    inp = inp or {}
    if name == "Bash":  # drop the `cd "<project>" &&` prefix every worker adds; first line only (heredocs)
        cmd = re.sub(r'^\s*cd\s+("[^"]*"|\S+)\s*(&&|;)\s*', "", inp.get("command", "")).strip()
        lines = cmd.splitlines() or [""]
        return lines[0] + (" …" if len(lines) > 1 else "")
    if name == "SubagentHandback":
        return inp.get("message", "")
    for k in ("file_path", "path", "notebook_path"):
        if inp.get(k):
            return os.path.basename(inp[k]) + (f"  ({inp['pattern']})" if inp.get("pattern") else "")
    if inp.get("pattern"):
        return inp["pattern"]
    if inp.get("url"):
        return inp["url"]
    if inp.get("query"):
        return inp["query"]
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:3])


def activity(plan, limit=14):
    """Per task: the latest steps from that subagent's own transcript (tool calls and short notes)."""
    import glob
    sdir = plan.get("session_dir")
    if not sdir:
        return {}
    tasks = {t["id"]: t for t in plan.get("tasks", [])}
    latest = {}  # task id -> newest transcript (a retry produces a second one)
    for meta in glob.glob(os.path.join(sdir, "subagents", "*.meta.json")):
        try:
            tid = task_id(json.load(open(meta)).get("description"), tasks)
        except (OSError, ValueError):
            continue
        tr = meta.removesuffix(".meta.json") + ".jsonl"
        if tid and os.path.exists(tr) and (tid not in latest or os.path.getmtime(tr) > os.path.getmtime(latest[tid])):
            latest[tid] = tr
    out = {}
    for tid, tr in latest.items():
        steps, turns, tokens, model = [], 0, 0, None
        for line in open(tr, encoding="utf-8"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message", {})
            model = m.get("model") or model
            turns += 1
            tokens += (m.get("usage") or {}).get("output_tokens", 0)
            ts = (d.get("timestamp") or "")[11:19]
            for c in m.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    kind = "report" if c.get("name") == "SubagentHandback" else c.get("name")
                    lim = 1200 if kind == "report" else 220
                    steps.append({"t": ts, "kind": kind, "text": summarize_tool(c.get("name"), c.get("input"))[:lim]})
                elif c.get("type") == "text" and c.get("text", "").strip():
                    steps.append({"t": ts, "kind": "say", "text": c["text"].strip().replace("\n", " ")[:220]})
        out[tid] = {"steps": steps[-limit:], "turns": turns, "out_tokens": tokens, "model": model,
                    "total_steps": len(steps)}
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/activity.json"):
            try:
                body = json.dumps(activity(load()), ensure_ascii=False).encode()
            except (OSError, ValueError):
                body = b"{}"
            ctype = "application/json"
        elif self.path.startswith("/plan.json"):
            try:
                body = open(PLAN, "rb").read()
            except FileNotFoundError:
                body = b'{"goal":"waiting for .orchestra/plan.json","tasks":[]}'
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body = open(os.path.join(HERE, "dashboard.html"), "rb").read()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def cmd_serve(port=8765, exact=False):
    socketserver.ThreadingTCPServer.allow_reuse_address = True  # rebind right after a restart (TIME_WAIT)
    for p in ([port] if exact else range(port, port + 20)):  # skip ports already in use
        try:
            srv = socketserver.ThreadingTCPServer(("127.0.0.1", p), Handler)
        except OSError:
            continue
        print(f"dashboard: http://127.0.0.1:{p}  (plan: {PLAN})", flush=True)
        srv.serve_forever()
    sys.exit("no free port")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "serve":
        cmd_serve(int(a[1]) if len(a) > 1 else 8765, exact="--exact" in a)
    elif a[0] == "set" and len(a) >= 3:
        cmd_set(a[1], a[2], a[3] if len(a) > 3 else None)
    elif a[0] == "hook":
        raw = sys.stdin.read()
        if os.environ.get("ORCH_HOOK_LOG"):
            with open(os.environ["ORCH_HOOK_LOG"], "a") as f:
                f.write(raw.strip() + "\n")
        try:
            msg = cmd_hook(json.loads(raw))
            if isinstance(msg, Deny):
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                         "permissionDecisionReason": str(msg)}}, ensure_ascii=False))
            elif msg:
                print(json.dumps({"systemMessage": msg}, ensure_ascii=False))
        except Exception as e:  # a broken board must never block tool calls
            print(f"orch hook: {e}", file=sys.stderr)
        sys.exit(0)
    elif a[0] == "board":
        cmd_board()
    elif a[0] == "briefs":
        cmd_briefs()
    elif a[0] == "approve":
        cmd_approve(a[2] if a[1:2] == ["--auto"] and len(a) > 2 else None)
    elif a[0] == "watch":
        if len(a) > 1:  # watch a run in another folder: orch.py watch <project folder>
            PLAN = os.path.join(os.path.abspath(os.path.expanduser(a[1])), ".orchestra", "plan.json")
        cmd_watch()
    elif a[0] == "statusline":
        cmd_statusline(a[2:] if a[1:2] == ["--"] else [])
    elif a[0] == "check":
        try:
            check(load())
        except AssertionError as e:
            sys.exit(f"invalid plan: {e}")
        print("plan ok")
    else:
        sys.exit(__doc__)
