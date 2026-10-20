#!/usr/bin/env python3
"""Queue of quest status changes clicked on the Quest Log card.

questcard writes every click to pending.json (next to this file) and drops the entry once Notion
confirms it (only when the card has its own Notion token). The scheduled Quest Log tasks apply what is
left through the Notion connector:

  python3 pending.py list                print the queued changes as JSON: [{id, status, date?, title, at}, ...]
                                         (date = move the quest to that day, "YYYY-MM-DD")
                                         (marks them as claimed, so an undo on an open card is queued
                                         instead of silently dropped while the task is writing them)
  python3 pending.py done ID AT [ID AT]  drop entries that were written to Notion (matched on id and at,
                                         so a newer click on the same quest is kept) and record them in
                                         applied.json
  python3 pending.py applied             print applied.json: [{id, status, date?, at, applied_at}, ...] for the
                                         last 14 days; `at` is when the user clicked (UTC)
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "pending.json")
LEDGER = os.path.join(HERE, "applied.json")
ALLOWED = {"✅ Cleared", "📜 To Do", "🔥 Grinding", "⏸ Delay"}
KEEP_DAYS = 14


def read_json(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f).get(key, [])
    except (FileNotFoundError, ValueError, AttributeError):
        return []
    return data if isinstance(data, list) else []


def write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".questlog-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def valid(a):
    return (isinstance(a, dict) and re.fullmatch(r"[0-9a-f]{32}", str(a.get("id", "")))
            and a.get("status") in ALLOWED
            and (a.get("date") is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(a.get("date")))))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list" and len(argv) == 2:
        raw = read_json(QUEUE, "actions")
        actions = [a for a in raw if valid(a)]
        if actions:
            now = time.time()
            for a in raw:
                if valid(a):
                    a["claimedAt"] = now
            write_json(QUEUE, {"version": 1, "actions": raw})
        print(json.dumps([{k: a[k] for k in ("id", "status", "date", "title", "at") if k in a} for a in actions], ensure_ascii=False))
        return 0
    if cmd == "done" and len(argv) >= 4 and len(argv[2:]) % 2 == 0:
        drop = set(zip(argv[2::2], argv[3::2]))
        raw = read_json(QUEUE, "actions")
        gone = [a for a in raw if isinstance(a, dict) and (a.get("id"), a.get("at", "")) in drop]
        kept = [a for a in raw if a not in gone]
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
        ledger = [e for e in read_json(LEDGER, "applied") if str(e.get("applied_at", "")) >= cutoff]
        ledger += [dict({"id": a["id"], "status": a.get("status"), "at": a.get("at", ""), "applied_at": now.isoformat()},
                        **({"date": a["date"]} if a.get("date") else {}), **({"title": a["title"]} if a.get("title") else {}))
                   for a in gone]
        write_json(LEDGER, {"version": 1, "applied": ledger})
        write_json(QUEUE, {"version": 1, "actions": kept})
        print(f"removed {len(gone)}, {len(kept)} left")
        return 0
    if cmd == "applied" and len(argv) == 2:
        print(json.dumps(read_json(LEDGER, "applied"), ensure_ascii=False))
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
