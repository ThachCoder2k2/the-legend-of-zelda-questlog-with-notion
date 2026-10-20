#!/usr/bin/env python3
"""Show a Quest Log card on the Mac.

Usage: python3 show_card.py [card.json] [--no-stamp] [--no-save] [--source NAME]
       (the JSON can also be piped on stdin)

Every card shown is also remembered (qlstate.py): runs/last-KIND.json is the offline fallback of the menu-bar
button, runs/status.json feeds its progress badge, and a "shown" stamp tells the catch-up and the Claude tasks'
late guard that this slot is done. --no-stamp (a manual open) leaves the stamp alone, --no-save (a cached card
being shown again) leaves the cache alone.

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

args = sys.argv[1:]
flags = {a for a in args if a.startswith("--")}
source = args[args.index("--source") + 1] if "--source" in args and args.index("--source") + 1 < len(args) else "claude-task"
files = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--source")]
data = json.load(open(files[0], encoding="utf-8")) if files else json.load(sys.stdin)
data.setdefault("timeoutSec", 1800)
data.setdefault("url", BOARD)


def remember(card):
    """Bookkeeping after the card is on screen. It never stops a card from showing, and each step stands alone:
    the stamp matters more than the cache, so a cache that can't be written must not cost the stamp."""
    try:
        sys.path.insert(0, HERE)
        sys.dont_write_bytecode = True
        import qlstate
    except Exception as e:  # noqa: BLE001
        print("state not saved:", e, file=sys.stderr)
        return
    kind = card.get("kind")
    T = None
    try:
        T = qlstate.scheduled_day(kind, qlstate.now_local()) if kind in qlstate.TIMES else None
    except Exception as e:  # noqa: BLE001
        print("day not worked out:", e, file=sys.stderr)
    if T is not None and "--no-stamp" not in flags:
        try:
            qlstate.mark(T, kind)
        except Exception as e:  # noqa: BLE001
            print("stamp not written:", e, file=sys.stderr)
    if "--no-save" not in flags:
        try:
            qlstate.save_last(card, source)
        except Exception as e:  # noqa: BLE001
            print("card not saved:", e, file=sys.stderr)
        try:
            qlstate.write_today(card, T or qlstate.now_local().date())
        except Exception as e:  # noqa: BLE001
            print("today list not written:", e, file=sys.stderr)
        if T is not None:
            try:
                qlstate.write_status(card, T)
            except Exception as e:  # noqa: BLE001
                print("status not written:", e, file=sys.stderr)

try:  # card files are only read once at start-up: drop the ones from earlier days
    import re
    import time
    for name in os.listdir(tempfile.gettempdir()):
        old = os.path.join(tempfile.gettempdir(), name)
        # only the random names mkstemp makes ("questlog-x3hpdeaw.json"), never the Claude tasks' questlog-KIND-card.json
        if re.fullmatch(r"questlog-[A-Za-z0-9_]{8}\.json", name) and time.time() - os.path.getmtime(old) > 86400:
            os.remove(old)
except OSError:
    pass

fd, path = tempfile.mkstemp(prefix="questlog-", suffix=".json")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)

exe = os.path.join(HERE, "questcard")
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

if os.access(exe, os.X_OK) and os.path.exists(os.path.join(HERE, "card.html")):
    try:
        subprocess.Popen([exe, path], **quiet)
        remember(data)
        print("card shown:", data.get("title", ""))
        sys.exit(0)
    except OSError as e:  # a half-written or foreign binary (e.g. while it is being rebuilt): use the dialog instead
        print("questcard did not start:", e, file=sys.stderr)

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
try:
    subprocess.Popen(["osascript", "-e", script, data.get("title", "Quest Log"), body], **quiet)
except OSError as e:
    print("no card and no dialog could be shown:", e, file=sys.stderr)
    sys.exit(1)
remember(data)
print("fallback dialog shown:", data.get("title", ""))
