#!/usr/bin/env bash
# Install the orchestrate skill for Claude Code (user level).
# Run from the cloned folder:  bash install.sh
# Safe to run again: it backs up settings.json and skips anything already installed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CLAUDE="$HOME/.claude"
TARGET="$CLAUDE/skills/orchestrate"

mkdir -p "$CLAUDE/skills" "$CLAUDE/agents"

# 1. the skill lives at ~/.claude/skills/orchestrate (link it there if cloned elsewhere)
if [ "$HERE" != "$TARGET" ]; then
  if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "มีโฟลเดอร์ $TARGET อยู่แล้ว (ไม่ใช่ลิงก์) — ย้ายออกก่อนแล้วรันใหม่" >&2; exit 1
  fi
  ln -sfn "$HERE" "$TARGET"
  echo "✓ skill → $TARGET"
fi

# 2. the three worker roles
for a in researcher builder reviewer; do
  dest="$CLAUDE/agents/$a.md"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    cp "$dest" "$dest.bak-orchestrate"
    echo "  (สำรอง agent เดิมไว้ที่ $dest.bak-orchestrate)"
  fi
  ln -sf "$TARGET/agents/$a.md" "$dest"
  echo "✓ agent $a"
done

# 3. hooks + status line in settings.json (backup first, merge, never overwrite other hooks)
python3 - "$CLAUDE/settings.json" <<'PY'
import json, os, shutil, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
if os.path.exists(p):
    shutil.copy(p, p + ".bak-orchestrate")
cmd = 'python3 "$HOME/.claude/skills/orchestrate/orch.py" hook'
hooks = d.setdefault("hooks", {})
def ensure(event, matcher=None):
    arr = hooks.setdefault(event, [])
    if any("orchestrate/orch.py" in json.dumps(h) for h in arr):
        return False
    entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
    if matcher:
        entry = {"matcher": matcher, **entry}
    arr.append(entry)
    return True
added = [ensure("PreToolUse", "Agent|Task"), ensure("SubagentStop")]
sl = d.get("statusLine") or {}
base = sl.get("command", "")
if "orch.py\" statusline" not in base and "orch.py statusline" not in base:
    d["statusLine"] = {**sl, "type": "command", "refreshInterval": 1,
                       "command": 'python3 "$HOME/.claude/skills/orchestrate/orch.py" statusline' + (" -- " + base if base else "")}
    added.append(True)
json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
open(p, "a").write("\n")
print("✓ settings.json " + ("อัปเดตแล้ว (สำรองไว้ที่ settings.json.bak-orchestrate)" if any(added) else "มีครบอยู่แล้ว"))
PY

echo
echo "เสร็จแล้ว เปิด claude ใหม่ แล้วพิมพ์: /orchestrate <งานที่ต้องการ>"
echo "ต้องมีสกิล prompt-master ด้วย (lead ใช้เขียน prompt ให้ลูกทีม)"
