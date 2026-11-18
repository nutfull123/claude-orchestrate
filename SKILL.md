---
name: orchestrate
description: Lead a multi-step task as the head agent - break it into a task graph, pick the right model and effort for each task, write every worker prompt with the prompt-master skill, show the split and the prompts to the user for approval before anything is sent, then hand units to researcher, builder and reviewer subagents and show progress in the terminal and a live dashboard. Use when the user says orchestrate, แตกงาน, แบ่งงานให้ agent, lead the team, show the task board, or gives a task with several independent parts.
---

# Orchestrate

You are the lead (Opus · high). You plan, split, choose models, dispatch, gate and merge. Workers do the units.
Write every message to the user in the user's language (Thai if they wrote in Thai), including the one-line wave notes.
Script: `~/.claude/skills/orchestrate/orch.py` (call it with `python3`).

The **role** (`agent`) decides tools: `researcher` is read-only, `builder` writes code, `reviewer` checks without editing.
The **model and effort** are chosen per task, by you, from what the task needs. The agent files only hold defaults.

## Choosing a model per task

Pick the cheapest model that can do the task well, then raise effort only for reasoning depth, not for size.

| Task looks like | Recommend | Why |
|---|---|---|
| Find files, run a command and report, read one doc page, list facts | haiku · medium | Mechanical, answer is checkable |
| Sweep a large codebase or many sources and summarize | haiku · xhigh or sonnet · medium | Breadth, little judgment |
| Clear spec, 1–3 files, with tests | sonnet · high | Standard implementation |
| UI page, glue code, refactor with tests in place | sonnet · high | Many small decisions, low risk |
| Unclear spec, cross-cutting design, unknown root cause bug | opus · high | Needs judgment; a wrong turn costs more than the model |
| Security, money, data migration, concurrency, deletion | opus · high (builder and reviewer) | Mistakes are expensive or irreversible |
| Routine review of a small, tested diff | sonnet · high | Enough to catch ordinary mistakes |
| Final review gate, or review of risky code | opus · medium (risky: opus · high) | Independent, stronger check |
| Fix after a failed check (retry) | same model, effort +1 step; after a 2nd fail move up one model | Cheap first, escalate on evidence |

Signals to move **up**: vague requirements, many interacting files, no tests, external effects, earlier failure on this unit.
Signals to move **down**: exact instructions, single file, deterministic check, read-only.
Models: `haiku`, `sonnet`, `opus` (`fable` only if the user has access). Effort: `low`, `medium`, `high`, `xhigh`, `max`.

## What the user sees

- **Status line (always on, no action needed):** below the input box, refreshed every second, whenever `.orchestra/plan.json` exists in the current folder. A block panel: a progress bar, the goal, then each wave with one row per task — animated block, id, role, model·effort (✓/⚠ once the hook has read that subagent's transcript), state with elapsed time (`running 35s`, `checking`, `waits t2,t3`), title, and `→ note`. Keep titles short and set a useful note when you mark a task done, because the user reads both there. When every task is done it collapses to two lines.
- **Your replies:** the board table, as below. This is required, not optional: the user judges the run by it.
- **Side pane (optional):** `python3 ~/.claude/skills/orchestrate/orch.py watch` redraws the full board every second (e.g. in a tmux/crew pane).

## 1. Write the plan

Write `.orchestra/plan.json` in the project folder (create the folder). Every non-lead task carries `model`, `effort` and a one-line `why` tied to this task (not a generic rule):

```json
{
  "goal": "one line: what done looks like",
  "tasks": [
    {"id": "t1", "title": "Find where auth redirects are handled", "agent": "researcher", "model": "haiku", "effort": "medium",
     "why": "grep and read 2-3 files, answer is file:line", "deps": [], "status": "pending", "check": "list of file:line"},
    {"id": "t2", "title": "Fix open-redirect in handlers/auth.py", "agent": "builder", "model": "opus", "effort": "high",
     "why": "security bug, URL parsing edge cases", "deps": ["t1"], "status": "pending", "check": "pytest tests/test_auth.py exits 0"},
    {"id": "t3", "title": "Review the fix against the goal", "agent": "reviewer", "model": "opus", "effort": "high",
     "why": "security-sensitive change", "deps": ["t2"], "status": "pending"}
  ]
}
```

Rules for the graph:
- A dep exists only if the task reads the other task's output. "And then" is not a dep; tasks without one run in parallel.
- Each unit is one bounded job with one `check` that can fail (a test, a command, a required source line). "Looks good" is not a check.
- `lead` tasks are work you do yourself (merging, deciding, small code steps, pure transformations): no model fields.
- Always end with a `reviewer` task before declaring the goal done.

Validate: `python3 ~/.claude/skills/orchestrate/orch.py check` (it rejects a missing model, effort or why).

## 2. Write every worker prompt with prompt-master

Invoke the **`prompt-master`** skill (Skill tool) once, then write one prompt per non-lead task, following its rules. For each prompt the target tool is "a Claude Code subagent (`<agent>` role) running `<model>` at `<effort>` effort", and its intent comes from the task's title, check, files and deps. Each prompt must state: the one task, the files it may touch (and must not), the input it gets, the check that decides pass/fail, the exact output to report back, and "Done when:". Write each one, prompt text only, to `.orchestra/briefs/<id>.md`.

**Then a plain-Thai version for the user.** Next to every prompt write `.orchestra/briefs/<id>.th.md`: the same prompt translated into simple, everyday Thai that a non-programmer can follow — not word for word, but covering every instruction that changes what the worker does: what it builds, which files it may touch and must not touch, the rules it must follow, how it proves it is done (the check), and what it reports back. Short sentences or bullets, technical names (file names, commands) kept as they are. If several prompts share blocks (e.g. `<contract>`, `<rules>`), also write `.orchestra/briefs/shared.th.md` explaining those blocks once in the same style. `approve` refuses to run while any `.th.md` is missing.

A task that depends on another task's output cannot know that output yet: end its prompt with a line `## Inputs from earlier tasks` and leave it empty. At dispatch you append the real results **below** that line.

## 3. Show the split and the prompts, and let the user decide

Run `python3 ~/.claude/skills/orchestrate/orch.py briefs`. It prints chat-ready Markdown in this order:
1. **แผนการแบ่งงาน** — a table: task, who does it, model · effort, what it waits for, why that model; then the dispatch order (which tasks go out together).
2. **ส่วนที่ใช้ร่วมกัน** — blocks identical in several prompts (e.g. `<contract>`, `<rules>`, `<report>`), each shown once with the tasks that use it.
3. **One section per task** — first the plain-Thai explanation, then the real prompt in a code block, shared blocks replaced by `…ส่วนกลางด้านบน…`. Lead-only tasks are listed as having no prompt.
4. The approval question with its two choices.

**Your reply must contain that whole output verbatim as your message text** — not summarized, not shortened, not only inside a collapsed tool result. The user reads the prompts there (often on a phone through Remote Control) before deciding. Add at most 2 lines of your own (a risky task, a model you were unsure about). Only after the full text is in your reply, ask for the decision:
- Use the AskUserQuestion tool when available (options: อนุมัติ / แก้…), but never as a stand-alone summary: the prompts must already be in your message above it. Otherwise ask in text and **wait**.
- Apply the changes (plan.json and/or the brief files, re-running prompt-master for any rewritten prompt), run `briefs` again, and ask again until the user approves.
- On approval run `python3 ~/.claude/skills/orchestrate/orch.py approve`. This freezes the prompts.
- Only if the user said up front to go ahead without asking, run `orch.py approve --auto "<their words>"` instead, and still copy the `briefs` output into your reply so they can see it.

**Enforced:** the hook blocks any Agent call for a planned task until `approve` has run, and blocks it if the prompt you send does not start with that task's approved brief, character for character. If you need a different prompt, edit the brief, show it, and get approval again. If the user says "ถามทุกรอบ", show `briefs` and re-approve before every wave.

## 4. Run it

Repeat until every task is done:
1. Before each wave, write one line: which tasks go out in parallel and why they don't wait for each other.
2. Dispatch every pending task whose deps are all `done`, **in one message** (parallel Agent calls). For each call pass `subagent_type` = the task's agent, `model` = the task's model, `effort` = the task's effort, a `description` that **starts with the task id** (`t2 fix redirect`), and `prompt` = the approved `.orchestra/briefs/<id>.md` exactly, plus (for dependent tasks) the deps' results appended after it. For a retry, send the approved brief unchanged with the failure evidence and "fix this only" appended.
3. A hook prints `▶ t2 → builder [opus · high]` under each dispatch (`⚠ call sent …` if your call's model/effort differ from the plan: fix and resend), marks the task `running`, and when the subagent returns records the model and effort it actually ran on and sets `review`.
4. Judge each returned task's check yourself (rerun the test; do not trust the report alone). Pass → `orch.py set t2 done "short result"`. Fail → `orch.py set t2 failed "reason"`, raise model/effort per the table (update plan.json), and send back only that unit with the evidence and "fix this only". After 3 failures, stop: the plan is wrong - replan or ask the user.
5. Run `orch.py board` and copy it into your reply. A `⚠` in ACTUALLY RAN means the run used a different model or effort than planned: tell the user.
6. Never resend units that already passed. `lead` tasks: set `running`/`done` yourself.

If you add or split tasks mid-run, edit plan.json and run `check` again.

## 5. Finish

Approval from the user is needed only for irreversible steps (deleting data, migrations, pushing, sending). Ask once, at that step.
Your final reply **must start with** the output of `orch.py board` as a code block (every row, the ACTUALLY RAN column, and the "Why this model" lines), then: what was done, the reviewer verdict, and anything failed or skipped.

## Browser dashboard (opens automatically)

On the first dispatch the hook starts a dashboard server for this project and opens it in the browser (URL also appears on the `▶` line). It shows the orchestrator, each wave, edges colored by model, and per task a dropdown with live activity from that subagent's transcript, the brief you sent, why the model was chosen, the check, and plan vs actual. Mention the URL to the user once. Set `ORCH_NO_DASHBOARD=1` to turn it off.
