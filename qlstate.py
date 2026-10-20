"""Shared state of the Quest Log cards: time windows, "already shown" stamps, claim locks, the last-card
cache, the live/shadow mode and the menu-bar status. Python 3.9+ standard library only.

Used by report.py (local runner, catch-up, on-demand open) and show_card.py (every card that is shown, whether it
came from a Claude task or from report.py). Everything lives in the runs/ folder next to this file:

  YYYY-MM-DD-KIND.shown / .skipped   the card of that day was shown / deliberately skipped (a later one covers it)
  YYYY-MM-DD-KIND.claim              a run is working on it right now (O_EXCL lock, stale after 10 minutes)
  last-KIND.json                     the last card shown for each kind, plus "savedAt": the offline fallback
  status.json                        today's progress for the menu-bar button
  mode                               "live" (local runner shows the cards) or "shadow" (Claude tasks do)
"""
import fcntl
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.realpath(__file__))
RUNS = os.path.join(HERE, "runs")

TIMES = {"morning": (7, 30), "midday": (12, 0), "evening": (22, 0)}
# A run only counts inside its window (hours; 28 = 04:00 the next day), so a late wake-up never shows a stale card.
# The windows tile the day: a card never follows another within minutes. The Sunday evening report also ends at 22:30,
# when the weekly reset starts moving quests, so it never reports numbers the reset has already changed.
WEEKLY_AT = (22, 30)
WINDOW = {"morning": (7.0, 11.5), "midday": (11.5, 21.5), "evening": (21.5, 28.0)}
KINDS = ("morning", "midday", "evening", "weekly")


def now_local():
    return datetime.now()


def scheduled_day(kind, now):
    """The day a run is for, or None when `now` is outside the kind's window."""
    h = now.hour + now.minute / 60
    a, b = WINDOW[kind]
    if a <= h < b:
        T = now.date()
    elif a <= h + 24 < b:
        T = now.date() - timedelta(days=1)
    else:
        return None
    if kind == "evening" and T.isoweekday() == 7 and now >= datetime(T.year, T.month, T.day, *WEEKLY_AT):
        return None
    return T


def kind_for_now(now):
    """Which card the button opens at this time of day, and for which day."""
    h = now.hour + now.minute / 60
    if h < 4:
        return "evening", now.date() - timedelta(days=1)
    if h < 11.5:
        return "morning", now.date()
    if h < 21.5:
        return "midday", now.date()
    return "evening", now.date()


def slot_passed(kind, T, now):
    h, m = TIMES[kind]
    return now >= datetime(T.year, T.month, T.day, h, m)


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- stamps and claims

def stamp(T, kind):
    return os.path.join(RUNS, f"{T}-{kind}")


def is_done(T, kind):
    s = stamp(T, kind)
    return os.path.exists(s + ".shown") or os.path.exists(s + ".skipped")


def mark(T, kind, how="shown"):
    os.makedirs(RUNS, exist_ok=True)
    with open(stamp(T, kind) + "." + how, "w") as f:
        f.write(now_local().isoformat(timespec="seconds"))


def _claim_is_stale(p, ttl):
    """The run that took the claim is gone (its pid is dead) or has had it for longer than `ttl` seconds."""
    try:
        age = time.time() - os.path.getmtime(p)
        with open(p) as f:
            pid = int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return True
    if age > ttl:
        return True
    if pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True   # killed (Quit, a relaunch, bootout): it never got to release
        except OSError:
            pass          # alive, but not ours to signal
    return False


def claim(T, kind, ttl=600):
    """Take the right to build and show this card. False when another run holds it (or already did).
    The check and the takeover of a stale claim happen under flock, so of any number of simultaneous callers
    exactly one wins."""
    p = stamp(T, kind) + ".claim"
    os.makedirs(RUNS, exist_ok=True)
    with open(p + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(p):
            if not _claim_is_stale(p, ttl):
                return False
            try:
                os.remove(p)
            except OSError:
                pass
        with open(p, "w") as f:
            f.write(str(os.getpid()))
        return True


def err_stamp(now=None):
    """One "the card could not be built" notice per day, whichever kind hit the error."""
    return os.path.join(RUNS, f"{(now or now_local()).date()}.errshown")


def release(T, kind):
    """Drop our own claim only: a run that outlived its ttl must not delete the claim of the run that replaced it."""
    p = stamp(T, kind) + ".claim"
    try:
        with open(p) as f:
            if f.read().strip() != str(os.getpid()):
                return
        os.remove(p)
    except OSError:
        pass


def due(now):
    """[(kind, T)] of the cards that are due and unseen right now, oldest slot first."""
    out = []
    for kind in TIMES:
        T = scheduled_day(kind, now)
        if T is not None and slot_passed(kind, T, now) and not is_done(T, kind):
            out.append((kind, T))
    out.sort(key=lambda kt: datetime(kt[1].year, kt[1].month, kt[1].day, *TIMES[kt[0]]))
    return out


# ---------------------------------------------------------------- mode

def mode():
    try:
        with open(os.path.join(RUNS, "mode"), encoding="utf-8") as f:
            return "live" if f.read().strip() == "live" else "shadow"
    except OSError:
        return "shadow"


def set_mode(m):
    _write_atomic(os.path.join(RUNS, "mode"), "live" if m == "live" else "shadow")


# ---------------------------------------------------------------- user presence

def idle_seconds():
    """Seconds since the last keyboard or mouse input (a long idle time also covers a locked or sleeping screen)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0
    except Exception:
        return 0.0   # unknown: assume the user is there


def console_locked():
    """True while the screen is locked (the login window is up): nobody can see a card."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, text=True, timeout=10).stdout
        return bool(re.search(r"<key>IOConsoleLocked</key>\s*<true/>", out))
    except Exception:
        return False


def user_present():
    """At the Mac: input in the last 10 minutes and the screen not locked. A dark wake (Power Nap) at 07:31 is not presence."""
    return idle_seconds() <= 600 and not console_locked()


def touch_claim(T, kind):
    """Keep a long build's claim from expiring (a retrying scheduled run can take minutes)."""
    try:
        os.utime(stamp(T, kind) + ".claim", None)
    except OSError:
        pass


def card_open():
    try:
        return subprocess.run(["/usr/bin/pgrep", "-x", "questcard"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- last-card cache and menu-bar status

def save_last(card, source="card"):
    kind = card.get("kind")
    if kind not in KINDS:
        return
    saved = dict(card)
    for k in ("savedAt", "cached"):
        saved.pop(k, None)
    saved["savedAt"] = now_local().astimezone().isoformat(timespec="seconds")
    saved["source"] = source
    _write_atomic(os.path.join(RUNS, f"last-{kind}.json"), json.dumps(saved, ensure_ascii=False, indent=1))


def load_last(kind):
    try:
        with open(os.path.join(RUNS, f"last-{kind}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def newest_saved():
    """The most recently saved card of the daily kinds, or None."""
    best = None
    for kind in TIMES:
        c = load_last(kind)
        t = saved_at(c) if c else None
        if t and (best is None or t > best[0]):
            best = (t, c)
    return best[1] if best else None


def saved_at(card):
    try:
        return datetime.fromisoformat(card["savedAt"])
    except (KeyError, ValueError, TypeError):
        return None


def write_status(card, T=None):
    """Today's quest progress for the menu-bar button: only cards that carry a progress count (morning, evening)."""
    p = card.get("progress") or {}
    if card.get("kind") not in ("morning", "evening") or not p.get("total"):
        return
    T = T or now_local().date()
    _write_atomic(os.path.join(RUNS, "status.json"), json.dumps({
        "date": str(T), "kind": card["kind"], "done": int(p.get("done", 0)), "total": int(p["total"]),
        "updatedAt": now_local().astimezone().isoformat(timespec="seconds")}))


def write_today(card, T):
    """The quests of the card just shown, for the menu-bar button's status actions (runs/today.json, by day):
    morning / midday = the day's list (T), evening = tomorrow's plan (T + 1). Only items that carry a Notion page url."""
    kind = card.get("kind")
    sections = card.get("sections") or []
    if kind in ("morning", "midday"):
        sec = next((s for s in sections if s.get("heading") in (None, "", "Today")), None)
        target = T
    elif kind == "evening":
        sec = next((s for s in sections if str(s.get("heading") or "").startswith("Tomorrow")), None)
        target = T + timedelta(days=1)
    else:
        return
    items = []
    for it in (sec or {}).get("items") or []:
        m = re.search(r"[0-9a-f]{32}", str(it.get("url") or "").replace("-", ""))
        if not m or not it.get("title"):
            continue
        e = {"id": m.group(0), "title": str(it["title"]), "type": it.get("type") or "side", "status": it.get("status") or "📜 To Do"}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(it.get("date") or "")):
            e["date"] = it["date"]
        items.append(e)
    path = os.path.join(RUNS, "today.json")
    try:
        with open(path, encoding="utf-8") as f:
            days = (json.load(f).get("days") or {})
    except (OSError, ValueError, AttributeError):
        days = {}
    keep_from = str(date.fromisoformat(str(T)) - timedelta(days=2))
    days = {d: v for d, v in days.items() if d >= keep_from}
    days[str(target)] = {"kind": kind, "items": items, "updatedAt": now_local().astimezone().isoformat(timespec="seconds")}
    _write_atomic(path, json.dumps({"days": days}, ensure_ascii=False, indent=1))


def read_status():
    try:
        with open(os.path.join(RUNS, "status.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def age_text(then, now=None):
    """'08:16' for today, 'yesterday 22:00', or 'Mon 19/10 22:00'."""
    now = now or datetime.now(then.tzinfo)
    d = (now.date() - then.date()).days
    hm = f"{then:%H:%M}"
    if d == 0:
        return hm
    if d == 1:
        return "yesterday " + hm
    return f"{then:%a %d/%m} {hm}"
