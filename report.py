#!/usr/bin/env python3
"""Quest Log morning / midday / evening cards built straight from the Notion API.

Produces the same cards as the Claude scheduled tasks questlog-morning-report, questlog-evening-reminder (12:00)
and questlog-evening-report (their SKILL.md prompts are the spec), in a second, with no Claude tokens and with
the Claude app closed. Needs the Notion integration token in the login Keychain (README.md, "Saving done to
Notion"). Python 3.9+ standard library only.

  python3 report.py morning|midday|evening [options]
        apply the queued card clicks (pending.json) to Notion, read the board, show the card
    --dry-run              print the card JSON instead: no Notion writes, pending.json untouched, no card
                           (queued clicks are applied in memory, so the JSON matches a real run)
    --today YYYY-MM-DD     build the card for that day
    --fixture FILE         read the board from FILE (written by --save-fixture) instead of Notion; implies --dry-run
    --save-fixture FILE    also write what was read from Notion to FILE
    --scheduled            launchd run: skip when the Mac woke up too late for this card, or it was already shown
  python3 report.py shadow morning|midday|evening
        LaunchAgent before go-live: shows nothing, waits for the Claude task's card (/tmp/questlog-KIND-card.json),
        builds ours at that moment and logs the differences (runs/compare.log, runs/DATE-KIND.diff.txt)
  python3 report.py compare KIND [CLAUDE.json [LOCAL.json]]
        diff two cards (default: the Claude task's last card against a dry run now)
  python3 report.py now [KIND]         open a card right now (the menu-bar button): the live card, or the last saved one
                                       when Notion is unreachable. KIND defaults to the one for the time of day
                                       (morning / midday / evening / weekly = the last weekly card)
  python3 report.py catchup            LaunchAgent (at login and every 10 min): refresh the deadlines and show the urgent
                                       card if one is due; show the one card that is due and unseen once you are back at
                                       the Mac (not while another card is open); live mode only
  python3 report.py gate KIND          for the Claude tasks: "go DATE" or "skip: why" (outside the window, already shown,
                                       or the local runner is live and shows it)
  python3 report.py status             mode, token, which cards were shown today, the saved cards
  python3 report.py refresh            read the quests that have a Due date into runs/urgent.json (needs the token)
  python3 report.py apply              write the queued card clicks to Notion now (the menu bar's status actions run it)
  python3 report.py add -- "TITLE, WHEN"  create a deadline quest in Notion (the menu bar's "Add deadline…"); WHEN reads
                                       "tomorrow 9am", "fri 17:00", "in 3h", "26/10 10:30" (quickadd.py); --dry-run only parses
  python3 report.py urgent [tick|list|snooze MIN|clear] [--force] [--dry-run]
                                       deadlines: show the urgent card if a step of the ladder is due (urgent.py)
  python3 report.py install [--live]   write and load the LaunchAgents (07:30, 12:00, 22:00 + catch-up + menu-bar button);
                                       shadow mode by default, --live makes the local runner show the cards
  python3 report.py uninstall          unload and remove them
  python3 report.py check              token, Notion access, row counts

  Options for gate / catchup / now: --at "YYYY-MM-DD HH:MM" pretends it is that time (tests); catchup --dry-run only
  says what it would do; catchup --force runs even in shadow mode.
"""
import argparse
import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import pending  # noqa: E402  (pending.json / applied.json helpers next to this file)
import qlstate  # noqa: E402  (windows, shown stamps, claim locks, last-card cache, mode)
import quickadd  # noqa: E402  (the "Add deadline…" line: title, when)
import urgent  # noqa: E402  (deadlines: urgent.json, the escalation ladder)
from qlstate import TIMES, WINDOW, scheduled_day  # noqa: E402,F401

RUNS = os.path.join(HERE, "runs")
SHOW = os.path.join(HERE, "show_card.py")
PENDING = os.path.join(HERE, "pending.py")
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
QUESTS_DB = "3ed39b409ee7481398e1892238e0821a"      # 🗂️ Quest Log Data → Quests (collection://60fcffbd-0336-432e-bd7c-816b68e905a4)
QUESTLINES_DB = "3e46ad718caa4e06a1830c528b6f96e6"  # 🗂️ Quest Log Data → Questlines (collection://126443f4-aef3-4330-96d7-5b529fd08d22)
PAGE = "https://app.notion.com/p/"
CLAUDE_CARD = "/tmp/questlog-{}-card.json"
AGENT = "com.questlog.report."
BAR = "com.questlog.bar"
CATCHUP = "com.questlog.catchup"
KEEP_DAYS = 30

MAIN, SIDE, FUN = "⭐ Main Quest", "🧩 Side Quest", "🎮 Fun"
TODO, DELAY, GRINDING, CLEARED = "📜 To Do", "⏸ Delay", "🔥 Grinding", "✅ Cleared"
LOCKED = "🔒 Locked"
TYPE = {MAIN: "main", SIDE: "side", FUN: "fun"}
RANK = {MAIN: 0, SIDE: 1, FUN: 2}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def _now():
    """The local time as an aware datetime (one place, so the tests can set the clock)."""
    return datetime.now().astimezone()


def label(d):
    return f"{DAYS[d.weekday()]} {d:%d/%m}"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def day(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def parse_ts(v):
    """Aware datetime from an ISO timestamp ("Z", offset, or naive = UTC); None if unparseable."""
    s = str(v or "").replace("Z", "+00:00")
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if m:  # Python 3.9 only reads 3 or 6 fractional digits
        s = m.group(1) + ((m.group(2) or "")[:7].ljust(7, "0") if m.group(2) else "") + m.group(3)
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def local_day(v):
    t = parse_ts(v)
    return t.astimezone().date() if t else None


# ---------------------------------------------------------------- 2-Min Start text

COMMANDS = {"git", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "python", "python3", "node", "brew", "make",
            "docker", "curl", "ls", "cat", "grep", "head", "tail", "cd", "swiftc", "rg", "find", "chmod"}
SUBCOMMANDS = {
    "git": {"status", "diff", "add", "commit", "push", "pull", "log", "checkout", "switch", "branch", "stash",
            "restore", "reset", "fetch", "merge", "rebase", "init", "clone", "show", "tag", "rm", "mv"},
    "npm": {"run", "test", "install", "i", "ci", "start"}, "pnpm": {"run", "test", "install", "i", "dev", "build"},
    "yarn": {"run", "test", "install", "add", "dev", "build"}, "pip": {"install", "list", "freeze", "show"},
    "pip3": {"install", "list", "freeze", "show"}, "brew": {"install", "upgrade", "update", "list", "info"},
    "docker": {"compose", "run", "ps", "build", "logs", "exec"},
}


def _core(tok):
    core = tok.rstrip(".,;:!?")
    return core, core != tok


def _arglike(tok):
    return bool(tok) and (tok[0] in "-./~$" or "/" in tok or "=" in tok or tok.isdigit()
                          or re.search(r"\.\w{1,5}$", tok) is not None)


def _command_end(toks, i, piped=False):
    """Index of the last token of the shell command starting at toks[i], or None if it isn't one."""
    name, stop = _core(toks[i][1])
    if not piped and name not in COMMANDS:
        return None
    j, args = i, 0
    if name in SUBCOMMANDS and not piped:
        if stop or i + 1 >= len(toks) or _core(toks[i + 1][1])[0] not in SUBCOMMANDS[name]:
            return None
        j, args, stop = i + 1, 1, _core(toks[i + 1][1])[1]
    while not stop and j + 1 < len(toks):
        nxt, nstop = _core(toks[j + 1][1])
        if nxt in ("|", "&&") and j + 2 < len(toks):
            k = _command_end(toks, j + 2, piped=True)
            if k is None:
                break
            return k
        if not _arglike(nxt):
            break
        j, args, stop = j + 1, args + 1, nstop
    return j if args or piped else None


def wrap_shell(text):
    """Wrap shell commands in backticks, the deterministic version of what the Claude tasks do by judgement:
    'run git status --short | head -40.' -> 'run `git status --short | head -40`.'"""
    if "`" in text:
        return text
    toks = [(m.start(), m.group()) for m in re.finditer(r"\S+", text)]
    spans, i = [], 0
    while i < len(toks):
        j = _command_end(toks, i)
        if j is None:
            i += 1
            continue
        spans.append((toks[i][0], toks[j][0] + len(_core(toks[j][1])[0])))
        i = j + 1
    for a, b in reversed(spans):
        text = text[:a] + "`" + text[a:b] + "`" + text[b:]
    return text


def start_text(s):
    return wrap_shell((s or "").replace("\\|", "|").strip())


# ---------------------------------------------------------------- Notion

class NotionError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"Notion {code}: {msg}" if code > 0 else str(msg))
        self.code = code  # HTTP status, 0 = unreachable, -1 = no token


def read_token():
    """The integration token from the login Keychain (via /usr/bin/security, which owns the item: no prompt)."""
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "QuestLog Notion", "-a", "token", "-w"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


INTERACTIVE = False   # the button sets it: fail fast to the saved card instead of retrying for a minute


def notion(token, method, path, body=None, tries=4):
    data = None if body is None else json.dumps(body).encode()
    if INTERACTIVE:
        tries = 2
    for n in range(tries):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8 if INTERACTIVE else 20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("message", msg)
            except (ValueError, AttributeError):
                pass
            if e.code not in (409, 429, 500, 502, 503, 504) or n == tries - 1:
                raise NotionError(e.code, msg)
            try:
                wait = float(e.headers.get("Retry-After") or 2 ** n)
            except ValueError:
                wait = 2 ** n
            time.sleep(min(wait, 30))
        except (urllib.error.URLError, OSError) as e:
            if n == tries - 1 or INTERACTIVE:
                raise NotionError(0, f"Notion unreachable: {getattr(e, 'reason', e)}")
            time.sleep(2 ** n)


def query_all(token, db, body=None):
    pages, cursor = [], None
    while True:
        b = dict(body or {}, page_size=100, **({"start_cursor": cursor} if cursor else {}))
        r = notion(token, "POST", f"/databases/{db}/query", b)
        pages += r.get("results", [])
        cursor = r.get("next_cursor")
        if not r.get("has_more") or not cursor:
            return pages


def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich or [])


def quest_row(page):
    p = page.get("properties", {})
    get = lambda name, kind: (p.get(name) or {}).get(kind)  # noqa: E731
    name = lambda v: (v or {}).get("name")  # noqa: E731
    return {
        "id": page["id"].replace("-", ""),
        "title": plain(get("Quest", "title")),
        "type": name(get("Type", "select")),
        "status": name(get("Status", "status")),
        "date": ((get("Date", "date") or {}).get("start") or "")[:10] or None,
        "due": (get("Due", "date") or {}).get("start") or None,
        "energy": name(get("Energy", "select")),
        "start": plain(get("2-Min Start", "rich_text")),
        "questlines": [r["id"].replace("-", "") for r in get("Questline", "relation") or []],
        "edited": get("Edited", "last_edited_time") or page.get("last_edited_time"),
        "on_today": (get("On Today", "formula") or {}).get("string"),
    }


def urgent_items(rows):
    """The quests with a Due date that are still open (Locked ones are not yours to act on)."""
    return [{"id": r["id"], "title": r["title"], "due": r["due"], "type": TYPE.get(r["type"], "side"), "status": r["status"],
             "start": start_text(r["start"]), "date": r["date"]}
            for r in rows if r.get("due") and r["title"] and r["status"] != CLEARED and r["energy"] != LOCKED]


def sync_deadlines(rows):
    """Every card the local runner builds has read all the quests anyway: keep runs/urgent.json (the "!" badge) fresh."""
    try:
        urgent.save(urgent_items(rows))
    except Exception as e:  # noqa: BLE001  a deadline list problem must never stop a card
        log(f"deadlines not saved: {e!r}")


def refresh_deadlines():
    """Read the quests with a Due date from Notion into runs/urgent.json. Needs the token; 0 also when there is none
    (the Claude tasks fill the list through `urgent.py set` then)."""
    token = read_token()
    if not token:
        return 0
    try:
        pages = query_all(token, QUESTS_DB, {"filter": {"and": [
            {"property": "Due", "date": {"is_not_empty": True}},
            {"property": "Status", "status": {"does_not_equal": CLEARED}}]}})
    except NotionError as e:
        log(f"deadlines: {e}")
        return 1
    rows = [quest_row(pg) for pg in pages]
    items = urgent.save(urgent_items(rows))
    log(f"deadlines: {len(items)} open")
    return 0


def add_cmd(rest, args):
    """`report.py add -- "Submit visa form, tomorrow 9am"`: create the quest in Notion with its Due and put it on the
    deadline list at once. Exit 0 = added (one line on stdout), 2 = couldn't read the line, 3 = no Notion token,
    1 = Notion said no (the message is on stderr)."""
    now = _at(args).astimezone()
    try:
        title, due, has_time = quickadd.parse(" ".join(rest), now)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    props = {
        "Quest": {"title": [{"text": {"content": title}}]},
        "Type": {"select": {"name": SIDE}},
        "Status": {"status": {"name": TODO}},
        "Date": {"date": {"start": due.date().isoformat()}},   # planned for the day it is due; move it earlier on the board
        "Due": {"date": {"start": due.isoformat(timespec="seconds") if has_time else due.date().isoformat()}},
    }
    shown = urgent.due_label(due, now) if has_time else urgent.due_label(due, now).rsplit(" ", 1)[0] + " (any time)"
    if args.dry_run:
        print(json.dumps({"title": title, "due": props["Due"]["date"]["start"], "shown": shown}, ensure_ascii=False))
        return 0
    token = read_token()
    if not token:
        print("The Notion token is not saved yet, so I can't create the quest.", file=sys.stderr)
        return 3
    global INTERACTIVE
    INTERACTIVE = True
    try:
        page = notion(token, "POST", "/pages", {"parent": {"database_id": QUESTS_DB}, "properties": props})
    except NotionError as e:
        hint = (" Give the integration the Insert content capability (Notion, Settings, Connections, your integration)."
                if e.code in (401, 403) else "")
        print(f"Notion said no: {e}.{hint}", file=sys.stderr)
        return 1
    qid = page["id"].replace("-", "")
    try:
        urgent.save(urgent.load() + [{"id": qid, "title": title, "due": props["Due"]["date"]["start"], "type": "side",
                                      "status": TODO, "date": due.date().isoformat()}])
    except Exception as e:  # noqa: BLE001  the quest exists in Notion; the next refresh picks it up
        log(f"add: deadline list not updated: {e!r}")
    log(f"add: {title!r} due {props['Due']['date']['start']}")
    print(f"Added: {title} · due {shown}")
    return 0


def apply_cmd():
    """`report.py apply`: write the queued card clicks to Notion now (the menu-bar button runs it right after you change a
    quest's status there). Quiet and exit 0 without a token; Notion errors stay in the queue for the next card."""
    token = read_token()
    if not token:
        return 0
    global INTERACTIVE
    INTERACTIVE = True
    try:
        apply_queue(token)
    except Exception as e:  # noqa: BLE001
        log(f"apply: {e!r}")
        return 1
    return 0


def deadline_tick(now=None):
    """Refresh the list, then show the urgent card if a step of the ladder is due."""
    try:
        refresh_deadlines()
    except Exception as e:  # noqa: BLE001
        log(f"deadlines: unexpected {e!r}")
    try:
        return urgent.tick(now=now.astimezone() if now else None)
    except Exception as e:  # noqa: BLE001
        log(f"urgent: unexpected {e!r}")
        return 1


def fetch(token):
    """Every quest (the ⚔️ Today, 🗓️ This Week and 🪧 Quest Board views are filters of it) and the questlines."""
    pages = query_all(token, QUESTS_DB, {"sorts": [{"property": "Type", "direction": "ascending"},
                                                   {"property": "Date", "direction": "ascending"}]})
    qls = {}
    for pg in query_all(token, QUESTLINES_DB):
        pr = pg.get("properties", {})
        qls[pg["id"].replace("-", "")] = {"name": plain((pr.get("Questline") or {}).get("title")),
                                          "goal": plain((pr.get("Done means") or {}).get("rich_text"))}
    return {"read_at": datetime.now(timezone.utc).isoformat(), "on_today_date": date.today().isoformat(),
            "rows": [quest_row(pg) for pg in pages], "questlines": qls}


# ---------------------------------------------------------------- step 0: card clicks

def queued():
    return [a for a in pending.read_json(pending.QUEUE, "actions") if pending.valid(a)]


def apply_queue(token):
    """Write the queued card clicks to Notion (Status, plus Date for a move) and drop them via pending.py done."""
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, "queue.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # a scheduled run and the button must not write the same quest in turn
        _apply_queue_locked(token)


def _apply_queue_locked(token):
    r = subprocess.run([sys.executable, PENDING, "list"], capture_output=True, text=True, timeout=30)
    try:
        actions = json.loads(r.stdout or "[]")
    except ValueError:
        log(f"pending.py list failed: {r.stderr.strip()}")
        return
    for a in actions:
        if a.get("status") not in pending.ALLOWED:
            continue
        props = {"Status": {"status": {"name": a["status"]}}}
        if a.get("date"):
            props["Date"] = {"date": {"start": a["date"]}}
        try:
            notion(token, "PATCH", f"/pages/{a['id']}", {"properties": props})
        except NotionError as e:
            log(f"click on {a.get('title') or a['id']} stays queued: {e}")
            if e.code in (0, -1):  # offline or no token: every other click would fail the same way, slowly
                return
            continue
        subprocess.run([sys.executable, PENDING, "done", a["id"], a.get("at", "")], capture_output=True, timeout=30)
        log(f"applied click: {a.get('title') or a['id']} → {a['status']}" + (f" on {a['date']}" if a.get("date") else ""))


def overlay(rows, actions, ledger):
    """Dry run: what step 0 would change, in memory only."""
    by_id = {r["id"]: r for r in rows}
    now = datetime.now(timezone.utc).isoformat()
    for a in actions:
        if a["id"] in by_id:
            by_id[a["id"]]["status"] = a["status"]
            if a.get("date"):
                by_id[a["id"]]["date"] = a["date"]
        ledger.append(dict({"id": a["id"], "status": a["status"], "at": a.get("at", ""), "applied_at": now},
                           **({"date": a["date"]} if a.get("date") else {})))


# ---------------------------------------------------------------- views and items

def titled(rows):
    return [r for r in rows if (r["title"] or "").strip()]


def week_rows(rows):  # 🗓️ This Week: every quest with a Date
    return [r for r in rows if r["date"]]


def board_rows(rows):  # 🪧 Quest Board: undated and not cleared
    return [r for r in rows if not r["date"] and r["status"] != CLEARED]


def today_rows(data, T):
    """⚔️ Today = Notion's "On Today" formula; for another day (or without the formula) the prompts' fallback rule."""
    rows = titled(data["rows"])
    if data.get("on_today_date") == T.isoformat() and any(r.get("on_today") is not None for r in data["rows"]):
        return [r for r in rows if r.get("on_today") == "yes"]
    return [r for r in rows if r["date"] and (day(r["date"]) == T or (day(r["date"]) < T and r["status"] != CLEARED))]


def steps(shown, qid, pool):
    """The questline log: the shown quest, its unfinished steps (earliest first), then the latest cleared ones;
    at most 8, listed by Date, undated last."""
    members = [q for q in pool if qid in q["questlines"]]
    order = {q["id"]: n for n, q in enumerate(pool)}
    chosen, seen = [shown], {shown["id"]}

    def add(qs):
        for q in qs:
            if len(chosen) < 8 and q["id"] not in seen:
                chosen.append(q)
                seen.add(q["id"])

    add(sorted((q for q in members if q["status"] != CLEARED), key=lambda q: (q["date"] is None, q["date"] or "")))
    cleared = [q for q in members if q["status"] == CLEARED]
    add(sorted((q for q in cleared if q["date"]), key=lambda q: q["date"], reverse=True) + [q for q in cleared if not q["date"]])
    chosen.sort(key=lambda q: (q["date"] is None, q["date"] or "", order.get(q["id"], -1)))
    return [dict({"title": q["title"], "url": PAGE + q["id"]}, **({"date": q["date"]} if q["date"] else {}),
                 done=q["status"] == CLEARED) for q in chosen]


def item(r, data, pool, tags=None, **flags):
    it = {"type": TYPE.get(r["type"], "side"), "title": r["title"]}
    tags = list(tags or [])
    d = urgent.parse_due(r.get("due")) if r.get("status") != CLEARED else None
    if d:
        tags.append("Due " + urgent.due_label(d, datetime.now().astimezone()))
    if tags:
        it["tags"] = tags
    it.update({k: True for k, v in flags.items() if v})
    it["url"] = PAGE + r["id"]
    it["status"] = r["status"]
    if r["date"]:
        it["date"] = r["date"]
    if d:
        it["due"] = r["due"]   # the card warns before "Move to tomorrow" lands after it
    if start_text(r["start"]):
        it["start"] = start_text(r["start"])
    if r["questlines"]:
        ql = data["questlines"].get(r["questlines"][0])
        if ql and ql["name"]:
            it["questline"] = ql["name"]
            if ql.get("goal"):
                it["goal"] = ql["goal"]
        it["steps"] = steps(r, r["questlines"][0], pool)
    if r["energy"]:
        it["energy"] = r["energy"]
    return it


def steps_pool(data, skip_locked=False):
    rows = [r for r in titled(data["rows"]) if not (skip_locked and r["energy"] == LOCKED)]
    return week_rows(rows) + board_rows(rows)


# ---------------------------------------------------------------- the three cards

def build_morning(data, T, ledger):
    rows = today_rows(data, T)
    pool = steps_pool(data)
    todo = sorted((r for r in rows if r["status"] != CLEARED),
                  key=lambda r: (RANK.get(r["type"], 3), r["date"] is None, r["date"] or ""))
    done = [r for r in rows if r["status"] == CLEARED]
    items = []
    for r in todo + done:
        tags = (["↩︎ from yesterday"] if r["date"] and day(r["date"]) < T else []) + (["⏸ delayed"] if r["status"] == DELAY else [])
        items.append(item(r, data, pool, tags, live=r["status"] == GRINDING, done=r["status"] == CLEARED))
    card = {
        "kind": "morning", "badge": "☀️", "eyebrow": f"Morning briefing · {label(T)}", "title": f"Today · {label(T)}",
        "subtitle": f"{plural(len(todo), 'quest')} to do · win the day = clear 1 ⭐",
        "progress": {"done": len(done), "total": len(rows)},
        "sections": [{"heading": "Today", "items": items,
                      "empty": "No quests today. Open the board and pull one from 🪧 Quest Board in as ⭐."}],
    }
    first = next((r for r in todo if r["type"] == MAIN), todo[0] if todo else None)
    if first and start_text(first["start"]):
        card["callout"] = {"label": "2-minute start", "text": start_text(first["start"])}
    card["note"] = "⭐ first, 🎮 after. Use a quest's status slot to update it."
    card["timeoutSec"] = 1800
    return card


def build_midday(data, T, ledger):
    rows = [r for r in today_rows(data, T) if r["status"] != CLEARED]
    rows.sort(key=lambda r: (RANK.get(r["type"], 3), 0 if r["type"] == MAIN and r["status"] == GRINDING else 1))
    pool = steps_pool(data)
    card = {"kind": "midday", "badge": "🕛", "eyebrow": f"Midday check · {label(T)}"}
    if not rows:
        card.update(title="All clear for today!", subtitle="✅ Enjoy 🎮 tonight, and stop on time.", sections=[])
    else:
        items = [item(r, data, pool, ["↩︎ from yesterday"] if day(r["date"]) and day(r["date"]) < T else None,
                      live=r["status"] == GRINDING) for r in rows]
        card.update(title=f"{plural(len(rows), 'quest')} left for tonight",
                    subtitle="Start the ⭐ quest as soon as you're home.", sections=[{"items": items}])
        if start_text(rows[0]["start"]):
            card["callout"] = {"label": "Tonight's 2-minute start", "text": start_text(rows[0]["start"])}
    card["timeoutSec"] = 1800
    return card


def latest(entries):
    return max(entries, key=lambda e: parse_ts(e.get("at")) or EPOCH)


def clear_day(r, ledger):
    hits = [e for e in ledger if e.get("id") == r["id"] and e.get("status") == CLEARED]
    return local_day(latest(hits).get("at")) if hits else local_day(r["edited"])


def moved_by_card(ledger, ids, T):
    """id -> day, for quests whose latest card click (any status) was today and was "Move to tomorrow": status To Do and a
    date of exactly T + 1. A later Undo or Clear is a newer entry and cancels it; a move to any other day (a next-week
    quest moved from the Sunday card) is not a move out of today."""
    by_id = {}
    for e in ledger:
        if e.get("id") in ids:
            by_id.setdefault(e["id"], []).append(e)
    out = {}
    for i, es in by_id.items():
        e = latest(es)
        if local_day(e.get("at")) == T and e.get("status") == TODO and day(e.get("date")) == T + timedelta(days=1):
            out[i] = day(e["date"])
    return out


def build_evening(data, T, ledger):
    TM = T + timedelta(days=1)
    rows = [r for r in titled(data["rows"]) if r["energy"] != LOCKED]
    week = week_rows(rows)
    pool = week + board_rows(rows)
    A = [r for r in week if day(r["date"]) == T]
    a_ids = {r["id"] for r in A}
    E = [r for r in rows if r["status"] == CLEARED and r["id"] not in a_ids and clear_day(r, ledger) == T]
    e_ids = {r["id"] for r in E}
    moved = moved_by_card(ledger, {r["id"] for r in rows}, T)
    M = [r for r in rows if r["id"] in moved and r["id"] not in a_ids and r["id"] not in e_ids]   # on today's plan, moved out by the card
    moved = {i: d for i, d in moved.items() if i in {r["id"] for r in M}}
    today = A + E + M
    X, Y = sum(r["status"] == CLEARED for r in A) + len(E), len(today)
    main_ok = any(r["type"] == MAIN and r["status"] == CLEARED and clear_day(r, ledger) == T for r in rows)
    delayed = [r for r in week if r["status"] == DELAY]
    unfinished = [r for r in week if day(r["date"]) <= T and r["status"] not in (CLEARED, DELAY)]
    # a Main cleared today counts once, for today: never also for its own (earlier) Date
    mains = {r["date"] for r in week if r["type"] == MAIN and r["status"] == CLEARED and clear_day(r, ledger) != T}
    streak, d = (1 if main_ok else 0), T - timedelta(days=1)
    while d.isoformat() in mains:
        streak, d = streak + 1, d - timedelta(days=1)

    plan, seen = [], set()
    for r in [r for r in week if day(r["date"]) == TM and r["status"] != CLEARED] + delayed + unfinished:
        if r["id"] not in seen:
            plan.append(r)
            seen.add(r["id"])
    plan.sort(key=lambda r: RANK.get(r["type"], 3))
    carried = {r["id"] for r in unfinished}

    cutoff = min(_now(), datetime(T.year, T.month, T.day, 23, 59).astimezone())   # a report for an earlier day judges by its end

    def past_due(r):
        d = urgent.parse_due(r.get("due"))
        return bool(d) and r["status"] != CLEARED and d <= cutoff

    def today_tags(r):
        return ((["→ rolls to tomorrow"] if r["id"] in carried else []) + (["⏸ delayed to tomorrow"] if r["status"] == DELAY else [])
                + ([f"→ moved to {label(moved[r['id']])}"] if r["id"] in moved else [])
                + (["⚠ past its deadline"] if past_due(r) else []))

    def plan_tags(r):
        return (["↩︎"] if r["id"] in carried else []) + (["⚠ past its deadline"] if past_due(r) else [])

    m, f = sum(r["type"] == MAIN for r in plan), sum(r["type"] == FUN for r in plan)
    card = {
        "kind": "evening", "badge": "🌙", "eyebrow": f"Evening report · {label(T)}",
        "title": f"Tomorrow: {m} ⭐ + {f} 🎮" if plan else "Nothing planned for tomorrow",
        "subtitle": "Today is won. Sleep on time 🛌" if main_ok else "No worries, ⭐ first tomorrow. Sleep on time 🛌",
    }
    if Y:
        card["progress"] = {"done": X, "total": Y}
    card["stats"] = [{"icon": "✅", "value": f"{X}/{Y}", "label": "quests"},
                     {"icon": "⭐", "value": "✅" if main_ok else "❌", "label": "main"},
                     {"icon": "⏸", "value": str(len({r["id"] for r in delayed} | set(moved))), "label": "moved"},
                     {"icon": "🔥", "value": str(streak), "label": "day streak"}]
    card["sections"] = [
        {"heading": "Today", "items": [item(r, data, pool, today_tags(r), done=r["status"] == CLEARED) for r in today]},
        {"heading": f"Tomorrow · {label(TM)}", "items": [item(r, data, pool, plan_tags(r)) for r in plan],
         "empty": "Nothing yet. Open the board and plan 1 ⭐ for tomorrow."},
    ]
    first_main = next((r for r in plan if r["type"] == MAIN), None)
    if first_main and start_text(first_main["start"]):
        card["callout"] = {"label": "Tomorrow's 2-minute start", "text": start_text(first_main["start"])}
    if T.isoweekday() == 7:   # the weekly reset runs at 22:30, after this report
        card["note"] = "🗓️ The weekly reset runs at 22:30 tonight and plans next week."
    elif not any(r["type"] == MAIN and day(r["date"]) == TM for r in week):
        card["note"] = "⚠️ Tomorrow has no ⭐ Main yet: plan one on the board (or run the weekly reset to catch up)."
    card["timeoutSec"] = 1800
    return card


BUILD = {"morning": build_morning, "midday": build_midday, "evening": build_evening}
EYEBROW = {"morning": ("☀️", "Morning briefing"), "midday": ("🕛", "Midday check"), "evening": ("🌙", "Evening report")}


def make_card(kind, T, dry, fixture=None, save_fixture=None):
    extra = []
    if fixture:
        with open(fixture, encoding="utf-8") as f:
            data = json.load(f)
    else:
        token = read_token()
        if not token:
            raise NotionError(-1, "no Notion token in the Keychain")
        if not dry:
            apply_queue(token)
        data = fetch(token)
        if not dry:
            sync_deadlines(data["rows"])
        if save_fixture:
            with open(save_fixture, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
    if dry or fixture:
        overlay(data["rows"], queued(), extra)
    data["rows"].sort(key=lambda r: (RANK.get(r["type"], 3), r["date"] is None, r["date"] or ""))
    ledger = [e for e in pending.read_json(pending.LEDGER, "applied") if isinstance(e, dict)] + extra
    return BUILD[kind](data, T, ledger)


def offline_card(kind, T, err):
    why = {-1: "No Notion token in the Keychain yet (README: Saving done to Notion).",
           0: "Notion can't be reached right now (offline?).",
           401: "Notion rejected the token: save a new one in the Keychain (README).",
           404: "The integration can't see the Quest Log databases: connect it to 🗂️ Quest Log Data.",
           -2: "Something unexpected went wrong while building the card. Details are in QuestLog/runs/."}
    badge, name = EYEBROW[kind]
    return {"kind": kind, "badge": badge, "eyebrow": f"{name} · {label(T)}", "title": "Couldn't read the Quest Log",
            "subtitle": why.get(err.code, str(err)), "sections": [],
            "note": "Open the board for today's quests. Logs: QuestLog/runs/ (launchd-%s.log, launchd-catchup.log, bar.log)" % kind,
            "timeoutSec": 300, "display": "compact"}


# ---------------------------------------------------------------- runs

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def prune_runs():
    cutoff = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(RUNS) if os.path.isdir(RUNS) else []:
        p = os.path.join(RUNS, name)
        if re.match(r"\d{4}-\d\d-\d\d-", name) and os.path.getmtime(p) < cutoff:
            os.remove(p)


def build_with_retry(kind, T, dry, args):
    for n in range(7 if args.scheduled else 1):  # after a wake-up the network can take a minute or two
        try:
            return make_card(kind, T, dry, args.fixture, args.save_fixture)
        except NotionError as e:
            if e.code != 0 or n == 6 or not args.scheduled:
                raise
            log(f"{kind}: {e}; retrying in 30 s")
            qlstate.touch_claim(T, kind)
            time.sleep(30)


def run(kind, args, now=None, presence=True):
    now = now or datetime.now()
    T = date.fromisoformat(args.today) if args.today else (scheduled_day(kind, now) or (None if args.scheduled else now.date()))
    if T is None:
        log(f"{kind}: skipped, {now:%H:%M} is outside its window")
        return 0
    dry = args.dry_run or bool(args.fixture)
    held = False
    if args.scheduled and not dry:
        if qlstate.mode() != "live" and not getattr(args, "force", False):
            log(f"{kind}: skipped, the mode is shadow (the Claude tasks show the cards)")
            return 0
        if qlstate.is_done(T, kind):
            log(f"{kind}: already shown for {T}")
            return 0
        why = _not_now(kind, T, now, presence)
        if why:
            log(f"{kind}: {why}; the catch-up retries every 10 min")
            return 0
        if not qlstate.claim(T, kind):  # the calendar agent and the catch-up can wake up together
            log(f"{kind}: another run is on it")
            return 0
        held = True
    real0 = datetime.now()
    try:
        # the clock for the check after the build: `now` plus the time the build took (equal to the wall clock in real use)
        return _run_locked(kind, T, dry, args, lambda: now + (datetime.now() - real0))
    finally:
        if held:
            qlstate.release(T, kind)


def _not_now(kind, T, now, presence=True):
    """Why a scheduled card should not be shown at this moment (None = go): it was shown meanwhile, its window is over,
    you are away or the screen is locked, or another card is open (an error notice does not count as one)."""
    if qlstate.is_done(T, kind):
        return "already shown"
    if scheduled_day(kind, now) != T:
        return f"{now:%H:%M} is outside its window now"
    if presence and not qlstate.user_present():
        return "you are away or the screen is locked"
    if presence and qlstate.card_open() and not os.path.exists(qlstate.err_stamp(now)):
        return "another card is open"
    return None


def _run_locked(kind, T, dry, args, now_fn=datetime.now):
    stamp = qlstate.stamp(T, kind)
    try:
        card = build_with_retry(kind, T, dry, args)
        if args.scheduled and not dry:  # a build can take minutes: is this card still wanted, and are you still there?
            why = _not_now(kind, T, now_fn(), presence=True)
            if why:
                log(f"{kind}: built, but {why}; not showing it now")
                return 0
    except Exception as e:  # noqa: BLE001  NotionError, or anything a builder raises on an odd row or a cut response
        if not isinstance(e, NotionError):
            e = NotionError(-2, f"unexpected error: {e!r}")
        log(f"{kind}: {e}")
        if dry:
            return 1
        card = offline_card(kind, T, e)
        if args.scheduled:
            # Tell the user once a day; stay unmarked so the next catch-up tick tries again, quietly.
            if os.path.exists(qlstate.err_stamp(now_fn())):
                return 1
            write_json(stamp + ".json", card)
            subprocess.run([sys.executable, SHOW, stamp + ".json", "--no-stamp", "--no-save"], check=False, timeout=60)
            open(qlstate.err_stamp(now_fn()), "w").close()
            return 1
        write_json(stamp + ".json", card)  # a manual run: show the notice, remember nothing
        subprocess.run([sys.executable, SHOW, stamp + ".json", "--no-stamp", "--no-save"], check=False, timeout=60)
        return 1
    if dry:
        print(json.dumps(card, ensure_ascii=False, indent=1))
        return 0
    write_json(stamp + ".json", card)
    # A manual `report.py KIND` is a preview: it must not use up the slot of the real scheduled card.
    flags = [] if args.scheduled else ["--no-stamp"] + (["--no-save"] if args.today else [])
    r = subprocess.run([sys.executable, SHOW, stamp + ".json", "--source", "local", *flags], check=False, timeout=60)
    if r.returncode != 0:
        log(f"{kind}: the card could not be shown (show_card.py exit {r.returncode}); not marked, the catch-up retries")
        return 1
    if args.scheduled:
        qlstate.mark(T, kind)
        qlstate.write_status(card, T)  # the menu-bar badge, with the day this card is for
    prune_runs()
    log(f"{kind}: card shown for {T}: {card.get('title', '')}")
    return 0


# ---------------------------------------------------------------- open any time, catch-up, gate, status

def _at(args):
    if getattr(args, "at", None):
        try:
            return datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        except ValueError:
            sys.exit('--at needs "YYYY-MM-DD HH:MM"')
    return datetime.now()


def _show(card, *flags):
    os.makedirs(RUNS, exist_ok=True)
    path = os.path.join(RUNS, "now.json")
    write_json(path, card)
    subprocess.run([sys.executable, SHOW, path, *flags], check=False, timeout=60)


def now_cmd(rest, args):
    """The menu-bar button: show a card right now, whatever the clock says and whether or not it was shown before."""
    now = _at(args)
    kind, T = qlstate.kind_for_now(now)
    if rest:
        if rest[0] not in qlstate.KINDS:
            sys.exit("usage: report.py now [morning|midday|evening|weekly]")
        kind = rest[0]
        T = now.date() if kind != "evening" or now.hour >= 4 else now.date() - timedelta(days=1)
    global INTERACTIVE
    INTERACTIVE = True
    if not qlstate.claim(date.today(), "now", ttl=120):  # a double click: the first one is already on it
        log("now: another open is in progress")
        return 0
    try:
        cached = qlstate.load_last(kind)
        saved = qlstate.saved_at(cached) if cached else None
        if kind == "weekly" and cached:  # written by the Claude task that plans the week; nothing to build here
            _show(cached, "--no-stamp", "--no-save")
            return 0
        if kind == "weekly":  # written by the Claude task (it plans the week), so there is nothing to build here
            _show(_no_card(kind, "No weekly card yet",
                           "The weekly reset runs on Sundays at 22:30 and its card is saved here afterwards."), "--no-stamp", "--no-save")
            return 0
        try:
            card = make_card(kind, T, False)
        except Exception as e:  # noqa: BLE001  the button must always show something
            if not isinstance(e, NotionError):
                e = NotionError(-2, f"unexpected error: {e!r}")
            log(f"now {kind}: {e}")
            fallback = cached or qlstate.newest_saved()  # this kind's last card, else the newest of any kind
            fsaved = qlstate.saved_at(fallback) if fallback else None
            if fallback and fsaved:
                card = relabel_saved(dict(fallback), fsaved.astimezone().replace(tzinfo=None), now)
                card["eyebrow"] = f"Saved {qlstate.age_text(fsaved)} · {card.get('eyebrow', kind.title())}"
                card["note"] = "Couldn't reach Notion, so this is the last saved card. Changes you make are queued."
            else:
                card = offline_card(kind, T, e)
            _show(card, "--no-stamp", "--no-save")
            return 0  # a card is on screen; the exit code is only non-zero when nothing could be shown
        seen = (kind, T) in qlstate.due(now)  # you opened the card that was due: the catch-up must not show it again
        _show(card, *([] if seen else ["--no-stamp"]), "--source", "local")
        return 0
    finally:
        qlstate.release(date.today(), "now")


def relabel_saved(card, saved, now):
    """A saved card says "Today" and "Tomorrow" for the day it was made. Shown the next day it would call today
    "Tomorrow", so name the days instead: "Yesterday · Mon 19/10", "Today · Tue 20/10". `saved` and `now` are naive local."""
    if card.get("kind") not in TIMES:
        return card
    base = qlstate.kind_for_now(saved)[1]   # the day the card is for (an evening report after midnight is still yesterday's)
    today = now.date()
    if base >= today:
        return card

    def heading(h):
        h = str(h or "")
        if h == "Today":
            return ("Yesterday · " if base == today - timedelta(days=1) else "") + label(base)
        if h.startswith("Tomorrow · "):
            rest = h[len("Tomorrow · "):]
            return ("Today · " + rest) if base + timedelta(days=1) == today else rest
        return h

    out = dict(card)
    out["sections"] = [dict(sec, heading=heading(sec.get("heading"))) for sec in card.get("sections") or []]
    title = str(card.get("title") or "")
    if title.startswith("Tomorrow") and base + timedelta(days=1) == today:
        out["title"] = "Today" + title[len("Tomorrow"):]
    return out


def _no_card(kind, title, text):
    return {"kind": kind, "eyebrow": "Quest Log", "title": title, "subtitle": text, "sections": [], "timeoutSec": 600,
            "display": "compact"}


def catchup(args):
    """After sleep, a shutdown or a late login: show the one card that is due and unseen, once the user is back."""
    now = _at(args)
    if not args.dry_run:
        deadline_tick(now if args.at else None)   # deadlines come first: the ladder has its own presence rules
    wanted = qlstate.due(now)
    if not wanted:
        log("catchup: nothing due")
        return 0
    mode = qlstate.mode()
    latest = wanted[-1]
    older = wanted[:-1]
    names = ", ".join(f"{k} {T}" for k, T in wanted)
    if args.dry_run:
        print(f"due: {names}\nmode: {mode}\nwould {'show' if mode == 'live' or args.force else 'only log (shadow)'} {latest[0]} {latest[1]}"
              + (f", skip {', '.join(k for k, _ in older)}" if older else ""))
        return 0
    if mode != "live" and not args.force:
        log(f"catchup: due {names}; shadow mode, the Claude tasks show the cards")
        return 0
    if not qlstate.user_present():
        log(f"catchup: due {names}; waiting until you are back at the Mac")
        return 0
    if qlstate.card_open() and not os.path.exists(qlstate.err_stamp(now)):
        log(f"catchup: due {names}; a card is open, not interrupting")
        return 0
    for kind, T in older:  # a newer card covers the older ones: never stack three on a late wake-up
        qlstate.mark(T, kind, "skipped")
        log(f"catchup: skipped {kind} {T}, {latest[0]} covers it")
    a = argparse.Namespace(scheduled=True, today=None, dry_run=False, fixture=None, save_fixture=None, force=args.force)
    return run(latest[0], a, now, presence=False)


def gate(kind, args):
    """For the Claude tasks, as their first step: should this run show a card at all? Always exit 0: the answer is the
    printed line ("go DATE", "wait SECONDS: why" or "skip: why"), and a non-zero exit would read as a failure."""
    now = _at(args)
    if kind == "weekly":
        return gate_weekly(now)
    T = scheduled_day(kind, now)
    if T is None:
        print(f"skip: {now:%H:%M} is outside the {kind} window; a later card covers it")
        return 0
    if qlstate.is_done(T, kind):
        print(f"skip: the {kind} card for {T} was already shown")
        return 0
    # The local runner only counts as "taking over" while it can actually build: with no token, or after it had to show
    # its error notice for this slot, the Claude task is the one that has to deliver the card.
    if qlstate.mode() == "live" and read_token() and not os.path.exists(qlstate.err_stamp(now)):
        print("skip: the local runner is live and shows this card")
        return 0
    print(f"go {T}")
    return 0


def gate_weekly(now):
    """The weekly reset moves every unfinished quest into next week, so it must run after the evening report of the same
    Sunday, in whatever order a late launch replays the two tasks. The evening window closes at 22:30 (qlstate.WEEKLY_AT),
    so from then on a report that was never shown is a report that is never shown, and the reset may go."""
    T = scheduled_day("evening", now)
    if T is not None and T.isoweekday() == 7 and not qlstate.is_done(T, "evening"):
        print(f"wait 120: the evening report for {T} has not been shown yet; check again in 2 minutes (give up after 40 minutes)")
        return 0
    print("go")
    return 0


def status_cmd():
    now = datetime.now()
    print(f"mode: {qlstate.mode()}  (live = the local runner shows the cards; shadow = the Claude tasks do)")
    missing = 'MISSING: security add-generic-password -s "QuestLog Notion" -a token -w'
    print("token: " + ("found" if read_token() else missing))
    print(f"idle: {qlstate.idle_seconds():.0f}s   card open: {qlstate.card_open()}")
    for kind in TIMES:
        T = scheduled_day(kind, now)
        s = qlstate.stamp(T, kind) if T else None
        state = "outside its window" if T is None else ("shown" if os.path.exists(s + ".shown") else
                "skipped" if os.path.exists(s + ".skipped") else "not shown yet")
        print(f"{kind:8} {TIMES[kind][0]:02d}:{TIMES[kind][1]:02d}  {state}")
    due = qlstate.due(now)
    print("due now: " + (", ".join(k for k, _ in due) if due else "nothing"))
    for kind in qlstate.KINDS:
        c = qlstate.load_last(kind)
        t = qlstate.saved_at(c) if c else None
        print(f"saved {kind:8} " + (f"{qlstate.age_text(t)} ({c.get('source', '?')})" if t else "none"))
    st = qlstate.read_status()
    print("badge: " + (f"{st['done']}/{st['total']} on {st['date']}" if st else "none"))
    return 0


# ---------------------------------------------------------------- comparison with the Claude tasks

def _empty(v):
    return v is None or v is False or v == "" or v == [] or v == {}


def norm(v):
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items() if not _empty(x)}
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v


def _short(v):
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= 140 else s[:137] + "..."


def diff(a, b, path="card", out=None):
    """Differences between our card (a) and the Claude task's (b), as readable lines."""
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            if k not in b:
                out.append(f"{path}.{k}: only ours = {_short(a[k])}")
            elif k not in a:
                out.append(f"{path}.{k}: only Claude's = {_short(b[k])}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        titles = lambda xs: [x.get("title") for x in xs if isinstance(x, dict)]  # noqa: E731
        if len(a) != len(b) or titles(a) != titles(b):
            out.append(f"{path}: {len(a)} vs {len(b)} entries\n    ours:     {_short(titles(a))}\n    Claude's: {_short(titles(b))}")
        keys = lambda xs: [x.get("url") or x.get("title") for x in xs if isinstance(x, dict)]  # noqa: E731
        if len(keys(a)) == len(a) == len(b) and keys(a) != keys(b) and sorted(map(str, keys(a))) == sorted(map(str, keys(b))):
            theirs = {str(k): x for k, x in zip(keys(b), b)}  # same entries in another order: compare them pairwise
            for n, x in enumerate(a):
                diff(x, theirs[str(keys(a)[n])], f"{path}[{n}]", out)
            return out
        for n in range(min(len(a), len(b))):
            diff(a[n], b[n], f"{path}[{n}]", out)
    elif a != b:
        out.append(f"{path}: ours {_short(a)} | Claude's {_short(b)}")
    return out


def compare_cards(ours, claude):
    return diff(norm(ours), norm(claude))


def shadow(kind, args):
    """Before go-live: build our card when the Claude task's card appears and log how they differ."""
    now = datetime.now()
    T = scheduled_day(kind, now)
    if T != now.date():
        log(f"shadow {kind}: skipped, {now:%H:%M} is outside its window")
        return 0
    started, path = time.time(), CLAUDE_CARD.format(kind)

    def fresh():
        try:
            return os.path.getmtime(path) >= started - 20 * 60
        except OSError:
            return False

    while not fresh() and time.time() < started + 45 * 60:
        time.sleep(20)
    found = fresh()
    if found:
        time.sleep(3)
    stamp = os.path.join(RUNS, f"{T}-{kind}")
    try:
        ours = build_with_retry(kind, T, True, args)
    except NotionError as e:
        line = f"{T} {kind}: our build failed: {e}"
    else:
        write_json(stamp + ".local.json", ours)
        if not found:
            line = f"{T} {kind}: no Claude card within 45 min (app closed?); ours saved"
        else:
            shutil.copyfile(path, stamp + ".claude.json")
            with open(path, encoding="utf-8") as f:
                diffs = compare_cards(ours, json.load(f))
            with open(stamp + ".diff.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(diffs) + "\n" if diffs else "match\n")
            line = f"{T} {kind}: " + ("MATCH" if not diffs else f"{len(diffs)} difference(s), see runs/{T}-{kind}.diff.txt")
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, "compare.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {line}\n")
    prune_runs()
    log("shadow " + line)
    return 0


def compare_cmd(rest, args):
    kind = rest[0] if rest and rest[0] in BUILD else sys.exit("usage: report.py compare morning|midday|evening [CLAUDE.json [LOCAL.json]]")
    claude_path = rest[1] if len(rest) > 1 else CLAUDE_CARD.format(kind)
    with open(claude_path, encoding="utf-8") as f:
        claude = json.load(f)
    if len(rest) > 2:
        with open(rest[2], encoding="utf-8") as f:
            ours = json.load(f)
    else:
        mt = datetime.fromtimestamp(os.path.getmtime(claude_path))
        T = date.fromisoformat(args.today) if args.today else (scheduled_day(kind, mt) or mt.date())
        ours = make_card(kind, T, True, args.fixture)
    diffs = compare_cards(ours, claude)
    print("\n".join(diffs) if diffs else "match")
    return 0 if not diffs else 3


# ---------------------------------------------------------------- setup

def check():
    token = read_token()
    if not token:
        have = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "QuestLog Notion", "-a", "token"],
                              capture_output=True).returncode == 0
        print("The Keychain item exists but its token is empty (the paste didn't take). Save it again:" if have else "No token. Save it:")
        print('  security add-generic-password -U -s "QuestLog Notion" -a token -w')
        return 1
    print("token: found in the Keychain")
    try:
        me = notion(token, "GET", "/users/me")
        print(f"integration: {me.get('name') or me.get('id')}")
        schema = notion(token, "GET", f"/databases/{QUESTS_DB}").get("properties", {})
        data = fetch(token)
    except NotionError as e:
        print(e)
        if e.code == 404:
            print("Connect the integration to the page 🗂️ Quest Log Data (••• → Connections).")
        return 1
    rows = data["rows"]
    print(f"quests: {len(rows)} ({len(titled(week_rows(rows)))} dated, {len(titled(board_rows(rows)))} on the Quest Board), "
          f"on today: {sum(r['on_today'] == 'yes' for r in rows)}, questlines: {len(data['questlines'])}")
    missing = [k for k in ("Quest", "Type", "Status", "Date", "Energy", "2-Min Start", "Questline", "Edited", "On Today")
               if k not in schema]
    if missing:
        print("missing properties: " + ", ".join(missing))
    return 1 if missing else 0


def _bootstrap(name, plist):
    domain = f"gui/{os.getuid()}"
    path = os.path.expanduser(f"~/Library/LaunchAgents/{name}.plist")
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootout", f"{domain}/{name}"], capture_output=True)
    r = None
    for _ in range(5):
        r = subprocess.run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(1)
    return r


def install(live):
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    os.makedirs(os.path.expanduser("~/Library/LaunchAgents"), exist_ok=True)
    os.makedirs(RUNS, exist_ok=True)
    env = {"PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8"}
    if live and not read_token():
        print("Not going live: no Notion token in the Keychain, so the local runner could not build any card.\n"
              'Add it (README: Saving "done" to Notion), run  report.py check, then install --live again.')
        return 1
    failed = []
    for kind, (h, m) in TIMES.items():
        name = AGENT + kind
        argv = [py, os.path.join(HERE, "report.py")] + ([kind, "--scheduled"] if live else ["shadow", kind])
        r = _bootstrap(name, {"Label": name, "ProgramArguments": argv, "StartCalendarInterval": {"Hour": h, "Minute": m},
                              "StandardOutPath": os.path.join(RUNS, f"launchd-{kind}.log"),
                              "StandardErrorPath": os.path.join(RUNS, f"launchd-{kind}.log"),
                              "EnvironmentVariables": env, "AbandonProcessGroup": True})
        print(f"{name}: {h:02d}:{m:02d} {'live' if live else 'shadow'}" + ("" if r.returncode == 0 else f" (load failed: {r.stderr.strip()})"))
        if r.returncode != 0:
            failed.append(name)
    # Catch-up: at login (a Mac that was off at 07:30) and every 10 minutes (a sleep, a late network, an away user).
    log_path = os.path.join(RUNS, "launchd-catchup.log")
    r = _bootstrap(CATCHUP, {"Label": CATCHUP, "ProgramArguments": [py, os.path.join(HERE, "report.py"), "catchup"],
                             "RunAtLoad": True, "StartInterval": 600, "StandardOutPath": log_path, "StandardErrorPath": log_path,
                             "EnvironmentVariables": env, "AbandonProcessGroup": True})
    print(f"{CATCHUP}: at login + every 10 min, {'live' if live else 'shadow (only logs)'}" + ("" if r.returncode == 0 else f" (load failed: {r.stderr.strip()})"))
    if r.returncode != 0:
        failed.append(CATCHUP)
    # The menu-bar button: always on, it only opens a card when clicked.
    bar = os.path.join(HERE, "questbar")
    if os.access(bar, os.X_OK):
        log_path = os.path.join(RUNS, "launchd-bar.log")
        r = _bootstrap(BAR, {"Label": BAR, "ProgramArguments": [bar], "RunAtLoad": True,
                             "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
                             "ProcessType": "Interactive", "StandardOutPath": log_path, "StandardErrorPath": log_path,
                             "EnvironmentVariables": env})
        print(f"{BAR}: menu-bar button" + ("" if r.returncode == 0 else f" (load failed: {r.stderr.strip()})"))
    else:
        print("questbar is not built: swiftc -O QuestBar.swift -o questbar -framework Cocoa -framework Carbon")
    if failed:  # the mode file stays as it was: run() refuses to show a scheduled card unless it says "live"
        print("Some agents did not load (" + ", ".join(failed) + "); the mode stays " + qlstate.mode() + ".")
        return 1
    qlstate.set_mode("live" if live else "shadow")  # last: only once everything loaded
    return 0


def uninstall():
    for name in [AGENT + k for k in TIMES] + [CATCHUP, BAR]:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{name}"], capture_output=True)
        path = os.path.expanduser(f"~/Library/LaunchAgents/{name}.plist")
        if os.path.exists(path):
            os.remove(path)
        print(f"{name}: removed")
    qlstate.set_mode("shadow")  # no agents left: the Claude tasks must not skip their cards because of a stale "live"
    return 0


def main_add(rest):
    """`add [--dry-run] [--at "YYYY-MM-DD HH:MM"] [--] TEXT…`, read by hand: the text is free-form and may start with a dash."""
    ns = argparse.Namespace(dry_run=False, at=None)
    while rest and rest[0] in ("--dry-run", "--at"):
        if rest[0] == "--dry-run":
            ns.dry_run, rest = True, rest[1:]
        elif len(rest) > 1:
            ns.at, rest = rest[1], rest[2:]
        else:
            sys.exit("--at needs a value")
    if rest and rest[0] == "--":
        rest = rest[1:]
    return add_cmd(rest, ns)


def main(argv):
    if len(argv) > 1 and argv[1] == "add":
        return main_add(argv[2:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, usage=argparse.SUPPRESS)
    ap.add_argument("cmd", choices=["morning", "midday", "evening", "shadow", "compare", "install", "uninstall", "check",
                                    "now", "catchup", "gate", "status", "refresh", "urgent", "apply"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today")
    ap.add_argument("--fixture")
    ap.add_argument("--save-fixture")
    ap.add_argument("--scheduled", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--at")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv[1:])
    if args.today:
        date.fromisoformat(args.today)
    if args.cmd in BUILD:
        return run(args.cmd, args, _at(args) if args.at else None)
    if args.cmd == "now":
        return now_cmd(args.rest, args)
    if args.cmd == "catchup":
        return catchup(args)
    if args.cmd == "gate":
        if not args.rest or args.rest[0] not in (*BUILD, "weekly"):
            ap.error("gate needs morning|midday|evening|weekly")
        return gate(args.rest[0], args)
    if args.cmd == "status":
        return status_cmd()
    if args.cmd == "refresh":
        return refresh_deadlines()
    if args.cmd == "apply":
        return apply_cmd()
    if args.cmd == "urgent":   # report.py urgent [tick|list|snooze MIN|clear] [--force] [--dry-run]
        extra = (["--force"] if args.force else []) + (["--dry-run"] if args.dry_run else [])
        return urgent.main(["urgent.py", *(args.rest or ["tick"]), *(extra if (args.rest or ["tick"])[0] == "tick" else [])])
    if args.cmd == "shadow":
        if not args.rest or args.rest[0] not in BUILD:
            ap.error("shadow needs morning|midday|evening")
        return shadow(args.rest[0], args)
    if args.cmd == "compare":
        return compare_cmd(args.rest, args)
    if args.cmd == "install":
        return install(args.live)
    if args.cmd == "uninstall":
        return uninstall()
    return check()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
