#!/usr/bin/env python3
"""Deadlines: the quests that have a Due date + time, and the escalation ladder that pops an urgent card as one nears.
Python 3.9+ standard library only.

  runs/urgent.json         {"updatedAt", "items": [{id, title, due, url, type, status, start?, date?}]}, soonest first.
                           Read by questbar (the "!" badge, the right-click list) and by `tick`. Written by
                           `report.py refresh` / every card the local runner builds (Notion API) or by `set` (the
                           Claude tasks, through the Notion connector).
  runs/urgent-state.json   which step of the ladder each quest has reached, and the snooze.

  python3 urgent.py set              replace the list from JSON on stdin ([{...}] or {"items": [...]}); extra keys are ignored
  python3 urgent.py list             print the list with countdowns
  python3 urgent.py tick [--force] [--dry-run]
                                     show the urgent card if a step of the ladder is due and unseen (questbar runs it every
                                     minute while a deadline is within 3 h; the catch-up agent every 10 minutes)
  python3 urgent.py snooze [MIN]     hold the alerts for MIN minutes (default 15); the card's Snooze button runs this
  python3 urgent.py clear            empty the list
  python3 urgent.py push setup|sample|status|enable|disable|test
                                     phone push through ntfy.sh while you are away from the Mac (off until `enable`)

Phone push (push.json, next to this file; never in git): while you are away from the Mac (no input for 10 minutes, or the screen
locked) and the Mac is awake, the 1 h, 15 min and overdue steps are sent to your phone through ntfy.sh instead of waiting for
a card. It is off until `push enable`. Nothing is sent but the notification itself (see `push sample`).

The ladder, by time left: 3 h heads-up (silent), 1 h (card + sound), 15 min (card + alert sound, again every 10 min),
overdue (card + alert sound, again every 30 min). A step shows once; a quest you are tracking (🔥 Grinding) gets the
steps but not the repeats; a quest you cleared on a card disappears at once. Alerts wait until you are at the Mac and never
interrupt an open card before the 15-minute step.
"""
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import pending  # noqa: E402
import qlstate  # noqa: E402

RUNS = qlstate.RUNS
FILE = os.path.join(RUNS, "urgent.json")
STATE = os.path.join(RUNS, "urgent-state.json")
LOCK = os.path.join(RUNS, "urgent.lock")
SHOW = os.path.join(HERE, "show_card.py")
PAGE = "https://app.notion.com/p/"
CLEARED, GRINDING = "✅ Cleared", "🔥 Grinding"
KEEP_OVERDUE = 3 * 86400            # a deadline missed by more than 3 days stops nagging
STAGE_AT = ((3 * 3600, 1), (3600, 2), (900, 3), (0, 4))   # seconds left -> step of the ladder
REPEAT = {3: 600, 4: 1800}          # steps that come back until you act
SNOOZE_MIN = 15
PUSH_FILE = os.path.join(HERE, "push.json")
PUSH_PRIORITY = {2: 3, 3: 4, 4: 5}          # ntfy: 3 default, 4 high, 5 max
PUSH_TAGS = {2: ["alarm_clock"], 3: ["warning"], 4: ["rotating_light"]}
PUSH_OVERDUE_EVERY = 3600                    # an overdue quest is pushed again every hour, at most PUSH_OVERDUE_MAX times in all
PUSH_OVERDUE_MAX = 3
EYEBROW = {1: "Heads-up", 2: "Deadline soon", 3: "Last call", 4: "Overdue"}
SOUND = {1: "", 2: "open", 3: "alert", 4: "alert"}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TYPES = {"main", "side", "fun"}


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} urgent: {msg}", file=sys.stderr, flush=True)


def now_aware():
    return datetime.now().astimezone()


# ---------------------------------------------------------------- time

def parse_due(raw):
    """Aware local datetime of a Notion date ("2026-10-20T17:00:00.000+07:00", "…Z", naive = local) or a plain date
    (= 23:59 that day); None if it is not a date."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, 23, 59).astimezone()
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)(\.\d+)?(.*)$", s.replace("Z", "+00:00"))
        if m:  # Python 3.9 only reads 3 or 6 fractional digits
            s = m.group(1) + ((m.group(2) or "")[:7].ljust(7, "0") if m.group(2) else "") + m.group(3)
        return datetime.fromisoformat(s).astimezone()
    except ValueError:
        return None


def stage_of(left):
    s = 0
    for limit, n in STAGE_AT:
        if left <= limit:
            s = n
    return s


def left_text(left):
    m = int(abs(left) // 60)
    if m >= 1440:
        t = f"{m // 1440}d {(m % 1440) // 60}h"
    elif m >= 60:
        t = f"{m // 60}h {m % 60:02d}m"
    else:
        t = f"{max(m, 1)} min"
    return f"{t} late" if left < 0 else f"in {t}"


def due_label(due, now):
    d = due.date()
    if d == now.date():
        day = "Today"
    elif d == now.date() + timedelta(days=1):
        day = "Tomorrow"
    else:
        day = f"{DAYS[d.weekday()]} {d:%d/%m}"
    return f"{day} {due:%H:%M}"


# ---------------------------------------------------------------- the list

def _hex_id(it):
    for v in (it.get("id"), it.get("url")):
        m = re.search(r"[0-9a-fA-F]{32}", str(v or "").replace("-", ""))
        if m:
            return m.group(0).lower()
    return None


def normalize(raw_items):
    """Clean items, soonest due first. Unusable ones (no id, title or due date) are dropped."""
    out = {}
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        qid, title, due = _hex_id(it), str(it.get("title") or "").strip(), parse_due(it.get("due"))
        if not (qid and title and due):
            continue
        e = {"id": qid, "title": title, "due": due.isoformat(timespec="seconds"), "url": PAGE + qid}
        t = str(it.get("type") or "").lower()
        e["type"] = t if t in TYPES else "side"
        if it.get("status") in pending.ALLOWED:
            e["status"] = it["status"]
        if it.get("start"):
            e["start"] = str(it["start"])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(it.get("date") or "")):
            e["date"] = it["date"]
        out[qid] = e
    return sorted(out.values(), key=lambda e: e["due"])


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return normalize(json.load(f).get("items"))
    except (OSError, ValueError, AttributeError):
        return []


def save(raw_items):
    items = normalize(raw_items)
    qlstate._write_atomic(FILE, json.dumps({"updatedAt": now_aware().isoformat(timespec="seconds"), "items": items},
                                           ensure_ascii=False, indent=1))
    return items


def latest_clicks():
    """id -> Status of the newest click on a card (queued or already written to Notion)."""
    best = {}
    for a in pending.read_json(pending.QUEUE, "actions") + pending.read_json(pending.LEDGER, "applied"):
        if isinstance(a, dict) and a.get("id") and a.get("status"):
            at = str(a.get("at", ""))
            if a["id"] not in best or at >= best[a["id"]][0]:
                best[a["id"]] = (at, a["status"])
    return {k: v[1] for k, v in best.items()}


def visible(items, now, clicks):
    """Without the quests you cleared on a card and the ones missed by more than KEEP_OVERDUE."""
    out = []
    for it in items:
        due = parse_due(it["due"])
        if due is None or (now - due).total_seconds() > KEEP_OVERDUE or clicks.get(it["id"]) == CLEARED:
            continue
        out.append(it)
    return out


# ---------------------------------------------------------------- the ladder

def due_alerts(now, items, state, clicks):
    """[(item, step)] whose step of the ladder is due and unseen."""
    if state.get("snoozeUntil", 0) > now.timestamp():
        return []
    out = []
    for it in items:
        left = (parse_due(it["due"]) - now).total_seconds()
        s = stage_of(left)
        if s == 0:
            continue
        rec = (state.get("items") or {}).get(it["id"])
        if rec and rec.get("due") != it["due"]:
            rec = None   # the deadline was edited: start the ladder again
        click = clicks.get(it["id"])
        tracking = click == GRINDING or (click is None and it.get("status") == GRINDING)
        if rec is None or s > rec.get("stage", 0):
            out.append((it, s))
        elif s in REPEAT and not tracking and now.timestamp() - rec.get("at", 0) >= REPEAT[s]:
            out.append((it, s))
    return out


def build_card(alerts, now):
    alerts = sorted(alerts, key=lambda a: a[0]["due"])
    step = max(s for _, s in alerts)
    items = []
    for it, _ in alerts:
        d = parse_due(it["due"])
        c = {"type": it["type"], "title": it["title"], "url": it["url"], "status": it.get("status") or "📜 To Do",
             "label": left_text((d - now).total_seconds()), "tags": ["Due " + due_label(d, now)]}
        c["due"] = it["due"]
        if it.get("date"):
            c["date"] = it["date"]
        if it.get("start"):
            c["start"] = it["start"]
        items.append(c)
    top = [it for it, st in alerts if st == step]   # the title is about the most urgent ones
    lead = top[0]
    lead_left = (parse_due(lead["due"]) - now).total_seconds()
    others = len(alerts) - 1
    if len(top) == 1:
        title = lead["title"]
        subtitle = f"Due {due_label(parse_due(lead['due']), now)} · {left_text(lead_left)}" + (f" · +{others} more" if others else "")
    else:
        title = f"{len(top)} deadlines are close"
        subtitle = f"First: {lead['title']} · {left_text(lead_left)}"
    return {
        "kind": "urgent", "display": "compact", "eyebrow": EYEBROW[step], "title": title, "subtitle": subtitle,
        "sections": [{"heading": "Overdue" if step == 4 else "Due soon", "items": items}],
        "note": f"Snooze holds the alerts for {SNOOZE_MIN} minutes.",
        "sound": SOUND[step], "timeoutSec": 1800 if step >= 3 else 900,
    }


def _state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(s):
    qlstate._write_atomic(STATE, json.dumps(s, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- phone push (ntfy.sh)

def push_config():
    """The settings when push is on and usable, else None."""
    try:
        with open(PUSH_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", str(cfg.get("topic", ""))):
        return None
    server = str(cfg.get("server") or "https://ntfy.sh").rstrip("/")
    if not server.startswith("https://"):
        return None
    return dict(cfg, server=server)


def push_payload(cfg, alerts, now):
    """The JSON that is sent for these alerts (the soonest, most urgent one leads; one notification per tick)."""
    alerts = sorted(alerts, key=lambda a: (-a[1], a[0]["due"]))
    lead, step = alerts[0]
    lines = []
    for it, _ in sorted(alerts, key=lambda a: a[0]["due"])[:4]:
        d = parse_due(it["due"])
        what = it["title"] if cfg.get("titles", True) else "A deadline"
        lines.append(f"{what} · {due_label(d, now)} · {left_text((d - now).total_seconds())}")
    if len(alerts) > 4:
        lines.append(f"+{len(alerts) - 4} more")
    body = {"topic": cfg["topic"], "title": "Quest Log · " + EYEBROW[step], "message": "\n".join(lines),
            "priority": PUSH_PRIORITY[step], "tags": PUSH_TAGS[step]}
    if cfg.get("titles", True) and lead.get("url"):
        body["click"] = lead["url"]   # tapping it opens the quest in Notion
    return body


def push_send(cfg, body):
    """POST the notification. True when ntfy accepted it; never raises (a push problem must not stop the card logic)."""
    req = urllib.request.Request(cfg["server"] + "/", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"push failed: {getattr(e, 'reason', e)}")
        return False


def push_away(alerts, state, now):
    """You are away: push the 1 h / 15 min / overdue steps that have not been pushed. Records them in state["push"]."""
    cfg = push_config()
    if not cfg:
        return False
    sent = state.setdefault("push", {})
    fresh = []
    for it, s in alerts:
        if s < 2:
            continue
        rec = sent.get(it["id"])
        if rec and rec.get("due") != it["due"]:
            rec = None   # the deadline was edited: start again
        if rec is None or s > rec.get("stage", 0):
            fresh.append((it, s))
        elif s == 4 and rec.get("n", 1) < PUSH_OVERDUE_MAX and now.timestamp() - rec.get("at", 0) >= PUSH_OVERDUE_EVERY:
            fresh.append((it, s))
    if not fresh:
        return False
    if not push_send(cfg, push_payload(cfg, fresh, now)):
        return False
    for it, s in fresh:
        old = sent.get(it["id"]) or {}
        again = old.get("due") == it["due"] and old.get("stage") == s
        sent[it["id"]] = {"due": it["due"], "stage": s, "at": now.timestamp(), "n": (old.get("n", 1) + 1) if again else 1}
    log("pushed to your phone: " + ", ".join(it["title"] for it, _ in fresh))
    return True


def push_cmd(args):
    sub = args[0] if args else "status"
    cfg = None
    try:
        with open(PUSH_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if sub == "setup":
        if cfg and cfg.get("topic"):
            print("push.json already has a topic; use `push status`.")
            return 0
        topic = "questlog-" + re.sub(r"[^A-Za-z0-9]", "", secrets.token_urlsafe(24))[:24]
        cfg = {"enabled": False, "topic": topic, "server": "https://ntfy.sh", "titles": True}
        qlstate._write_atomic(PUSH_FILE, json.dumps(cfg, indent=1))
        os.chmod(PUSH_FILE, 0o600)
        print(f"topic: {topic}\nserver: https://ntfy.sh\nstate: OFF (nothing is sent until `push enable`)")
        return 0
    if not cfg or not cfg.get("topic"):
        print("run `urgent.py push setup` first", file=sys.stderr)
        return 2
    if sub == "status":
        print(f"{'ON' if cfg.get('enabled') else 'OFF'} · server {cfg.get('server', 'https://ntfy.sh')} · topic {cfg['topic']} · "
              f"quest titles {'included' if cfg.get('titles', True) else 'left out'}")
        return 0
    if sub in ("enable", "disable"):
        cfg["enabled"] = sub == "enable"
        qlstate._write_atomic(PUSH_FILE, json.dumps(cfg, indent=1))
        print("push is now " + ("ON" if cfg["enabled"] else "OFF"))
        return 0
    if sub == "sample":   # what would be sent, for a made-up quest; no network
        now = now_aware()
        it = {"id": "a" * 32, "title": "Submit visa form", "due": (now + timedelta(minutes=14)).isoformat(timespec="seconds"),
              "url": PAGE + "a" * 32}
        shown = dict(cfg, enabled=True, topic="<your topic>")
        print("POST " + cfg.get("server", "https://ntfy.sh") + "/   (JSON)")
        print(json.dumps(push_payload(shown, [(it, 3)], now), ensure_ascii=False, indent=1))
        return 0
    if sub == "test":
        ok = push_send(dict(cfg, server=str(cfg.get("server") or "https://ntfy.sh").rstrip("/")),
                       {"topic": cfg["topic"], "title": "Quest Log · test", "message": "Phone push works.", "tags": ["white_check_mark"]})
        print("sent" if ok else "not sent")
        return 0 if ok else 1
    print("usage: urgent.py push setup|sample|status|enable|disable|test", file=sys.stderr)
    return 2


def tick(now=None, force=False, dry=False, presence=None):
    """Show the urgent card if a step of the ladder is due. 0 = nothing wrong (including "nothing to show")."""
    now = now or now_aware()
    os.makedirs(RUNS, exist_ok=True)
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        clicks = latest_clicks()
        everything = load()
        items = visible(everything, now, clicks)
        if len(items) != len(everything):
            save(items)   # the badge and the menu stop showing what you cleared
        state = _state()
        alerts = due_alerts(now, items, state, clicks)
        if not alerts:
            return 0
        step = max(s for _, s in alerts)
        card = build_card(alerts, now)
        if dry:
            print(json.dumps(card, ensure_ascii=False, indent=1))
            return 0
        if not force:
            here = qlstate.user_present() if presence is None else presence
            if not here:
                if push_away(alerts, state, now):
                    _save_state(state)
                log(f"step {step} is due; waiting until you are back at the Mac")
                return 0
            if step < 3 and qlstate.card_open():
                log(f"step {step} is due; a card is open, not interrupting")
                return 0
        path = os.path.join(RUNS, "urgent-card.json")
        qlstate._write_atomic(path, json.dumps(card, ensure_ascii=False))
        r = subprocess.run([sys.executable, SHOW, path, "--no-stamp", "--no-save", "--source", "urgent"], check=False, timeout=60)
        if r.returncode != 0:
            log(f"the card could not be shown (show_card.py exit {r.returncode}); will try again")
            return 1
        recorded = state.setdefault("items", {})
        for it, s in alerts:
            recorded[it["id"]] = {"due": it["due"], "stage": s, "at": now.timestamp()}
        state["items"] = {k: v for k, v in recorded.items() if k in {i["id"] for i in items}}
        _save_state(state)
        log(f"showed step {step}: " + ", ".join(it["title"] for it, _ in alerts))
        return 0


def snooze(minutes=SNOOZE_MIN, now=None):
    now = now or now_aware()
    os.makedirs(RUNS, exist_ok=True)
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        s = _state()
        s["snoozeUntil"] = (now + timedelta(minutes=minutes)).timestamp()
        _save_state(s)
    return s["snoozeUntil"]


def list_cmd():
    now = now_aware()
    items = visible(load(), now, latest_clicks())
    if not items:
        print("no deadlines")
    for it in items:
        d = parse_due(it["due"])
        print(f"{due_label(d, now):>16}  {left_text((d - now).total_seconds()):>14}  step {stage_of((d - now).total_seconds())}  {it['title']}")
    sn = _state().get("snoozeUntil", 0)
    if sn > now.timestamp():
        print(f"snoozed until {datetime.fromtimestamp(sn):%H:%M}")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "set" and len(argv) == 2:
        try:
            raw = json.load(sys.stdin)
        except ValueError as e:
            print(f"urgent set: not JSON ({e})", file=sys.stderr)
            return 2
        items = save(raw.get("items") if isinstance(raw, dict) else raw)
        print(f"{len(items)} deadline(s) saved")
        return 0
    if cmd == "list" and len(argv) == 2:
        return list_cmd()
    if cmd == "tick" and set(argv[2:]) <= {"--force", "--dry-run"}:
        return tick(force="--force" in argv, dry="--dry-run" in argv)
    if cmd == "snooze" and len(argv) <= 3:
        try:
            minutes = int(argv[2]) if len(argv) == 3 else SNOOZE_MIN
        except ValueError:
            return 2
        until = snooze(max(1, min(minutes, 24 * 60)))
        print(f"snoozed until {datetime.fromtimestamp(until):%H:%M}")
        return 0
    if cmd == "push":
        return push_cmd(argv[2:])
    if cmd == "clear" and len(argv) == 2:
        save([])
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
