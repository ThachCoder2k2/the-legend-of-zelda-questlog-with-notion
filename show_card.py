#!/usr/bin/env python3
"""Show a Quest Log card on the Mac.

Usage: python3 show_card.py card.json   (or pipe the JSON on stdin)

Opens the card (questcard + card.html, same folder). If that
binary is missing, falls back to a plain AppleScript dialog so the reminder is
never lost. Card fields are documented in README.md next to this file.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD = "notion://www.notion.so/3fe207ad5a6f81858d5fcde429bff7cf"
EMOJI = {"main": "⭐", "side": "🧩", "fun": "🎮"}

data = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else json.load(sys.stdin)
data.setdefault("timeoutSec", 1800)
data.setdefault("url", BOARD)

fd, path = tempfile.mkstemp(prefix="questlog-", suffix=".json")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)

exe = os.path.join(HERE, "questcard")
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

if os.access(exe, os.X_OK) and os.path.exists(os.path.join(HERE, "card.html")):
    subprocess.Popen([exe, path], **quiet)
    print("card shown:", data.get("title", ""))
    sys.exit(0)

# Fallback: plain dialog with the same content as text.
lines = []
if data.get("subtitle"):
    lines.append(data["subtitle"])
for s in data.get("stats", []):
    lines.append(f'{s.get("icon", "")} {s.get("value", "")} {s.get("label", "")}'.strip())
for sec in data.get("sections", []):
    if sec.get("heading"):
        lines += ["", sec["heading"]]
    for it in sec.get("items", []):
        label = f'{it["label"]} ' if it.get("label") else ""
        mark = "✅" if it.get("done") else EMOJI.get(it.get("type"), "•")
        lines.append(f'{mark} {label}{it.get("title", "")}')
if data.get("callout", {}).get("text"):
    lines += ["", "▶ " + data["callout"].get("label", "2-minute start") + ": " + data["callout"]["text"].replace("`", "")]
body = "\n".join(lines)[:1200]
script = (
    "on run argv\n"
    "set t to item 1 of argv\nset m to item 2 of argv\n"
    'set r to display dialog m with title t buttons {"Later", "Open board"} default button "Open board" giving up after 1800\n'
    f'if button returned of r is "Open board" then open location "{BOARD}"\n'
    "end run"
)
subprocess.Popen(["osascript", "-e", script, data.get("title", "Quest Log"), body], **quiet)
print("fallback dialog shown:", data.get("title", ""))
