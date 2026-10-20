"""Reads one line typed in the menu bar's "Add deadline…" box: "Submit visa form, tomorrow 9am" -> (title, due, has_time).
Python 3.9+ standard library only; no clock of its own (the caller passes `now`), so it is easy to test.

The title and the time are split at the last comma, or, without a comma, at the longest tail that reads as a time:

  Submit visa form, tomorrow 9am        Pay rent in 3h             Report, fri 17:00
  Call the bank, 26/10 10:30            Renew passport 2026-11-05  Gift, today 8pm
  Dentist, 17h30                        Essay, in 1h30m            Taxes, sun

Times: 9am  5:30pm  17:00  17h30  noon  midnight; days: today, tomorrow (tmr), mon … sun (the next one), dd/mm,
dd/mm/yyyy, yyyy-mm-dd; relative: "in 45m", "in 3h" (or just "3h"), "in 1h30m", "in 2d", "in 1w"; "17h" is 17:00. A time alone means the next time the
clock shows it. A day without a time is a date-only deadline (the whole day, i.e. until 23:59), like a Notion date.
"""
import re
from datetime import date, datetime, time, timedelta

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
HINT = ('Write the title, then when: "Submit visa form, tomorrow 9am", "Pay rent in 3h", "Report, fri 17:00", '
        '"Call the bank, 26/10 10:30".')

_TIME = r"(?:noon|midnight|\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)|\d{1,2}h\d{2})"
_DAY = r"(?:today|tomorrow|tmrw|tmr|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_DATE = r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
_ABS = re.compile(rf"^(?:(?P<day>{_DAY})|(?P<date>{_DATE}))?\s*(?:(?:at|@)\s*)?(?P<time>{_TIME})?$")
_REL = re.compile(r"^(?:in\s+)?(?P<body>(?:\d+(?:\.\d+)?\s*(?:minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\s*)+)$")
_REL_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)")
_CONNECTORS = {"by", "at", "due", "on", "for", "@", "-", "–", "before"}


def _clock(text):
    """'9am' '17:30' '17h' '5:30 pm' 'noon' -> datetime.time"""
    t = text.strip().lower()
    if t == "noon":
        return time(12, 0)
    if t == "midnight":
        return time(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t) or re.fullmatch(r"(\d{1,2})h(\d{2})()", t)
    if not m:
        raise ValueError(f'Couldn\'t read "{text}" as a time.')
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3) or None
    if ap:
        if not 1 <= h <= 12:
            raise ValueError(f'"{text}" is not a time (am/pm needs 1 to 12).')
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        raise ValueError(f'"{text}" is not a time.')
    return time(h, mi)


def parse_when(text, now):
    """(datetime with now's timezone, has_time). ValueError with a message that can be shown as it is."""
    s = re.sub(r"\s+", " ", text.strip().lower())
    if not s:
        raise ValueError("Say when it is due. " + HINT)
    tz = now.tzinfo
    s = re.sub(r"^(1[3-9]|2[0-3])h$", r"\1h00", s)   # "17h" is 17:00; a bare "3h" is three hours from now

    m = _REL.match(s)
    if m:
        delta = timedelta()
        for num, unit in _REL_PART.findall(m.group("body")):
            n = float(num)
            delta += (timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n) if unit.startswith("h")
                      else timedelta(days=n) if unit.startswith("d") else timedelta(weeks=n))
        if delta <= timedelta(0):
            raise ValueError("That is not in the future.")
        return (now + delta).replace(second=0, microsecond=0), True

    m = _ABS.match(s)
    if not m or not (m.group("day") or m.group("date") or m.group("time")):
        raise ValueError(f'Couldn\'t read "{text.strip()}" as a time. ' + HINT)
    at = _clock(m.group("time")) if m.group("time") else None
    today = now.date()
    if m.group("date"):
        parts = re.split(r"[/-]", m.group("date"))
        try:
            if len(parts[0]) == 4:
                d = date(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                year = int(parts[2]) if len(parts) == 3 else today.year
                year += 2000 if year < 100 else 0
                d = date(year, int(parts[1]), int(parts[0]))
                if len(parts) == 2 and d < today:
                    d = date(year + 1, int(parts[1]), int(parts[0]))
        except ValueError:
            raise ValueError(f'"{m.group("date")}" is not a date (day/month, e.g. 26/10).') from None
    elif m.group("day"):
        w = m.group("day")
        if w == "today":
            d = today
        elif w in ("tomorrow", "tmr", "tmrw"):
            d = today + timedelta(days=1)
        else:
            wd = DAYS[w[:3]]
            ahead = (wd - today.weekday()) % 7
            d = today + timedelta(days=ahead)
            if ahead == 0 and at is not None and datetime.combine(d, at, tz) <= now:
                d += timedelta(days=7)   # "mon 9am" on a Monday afternoon means next Monday
    else:   # a time alone: the next time the clock shows it
        d = today
        if datetime.combine(d, at, tz) <= now:
            d += timedelta(days=1)
    if at is None:
        if d < today:
            raise ValueError("That day has already passed.")
        return datetime.combine(d, time(23, 59), tz), False
    due = datetime.combine(d, at, tz)
    if due <= now:
        raise ValueError(f"That time has already passed ({due:%a %d/%m %H:%M}).")
    return due, True


def parse(text, now):
    """(title, due, has_time) from "title, when" or "title when"."""
    raw = re.sub(r"\s+", " ", text.strip())
    if not raw:
        raise ValueError(HINT)
    if "," in raw:
        title, when = raw.rsplit(",", 1)
        due, has_time = parse_when(when, now)
    else:
        words = raw.split(" ")
        found = None
        for k in range(min(5, len(words)), 0, -1):   # the longest tail that reads as a time wins
            try:
                found = (" ".join(words[:-k]), parse_when(" ".join(words[-k:]), now))
                break
            except ValueError:
                continue
        if not found:
            raise ValueError("Add when it is due. " + HINT)
        title, (due, has_time) = found
        parts = title.split(" ")
        while len(parts) > 1 and parts[-1].lower() in _CONNECTORS:
            parts.pop()
        title = " ".join(parts)
    title = title.strip(" ,-–")
    if not title:
        raise ValueError("Add a title before the time. " + HINT)
    if len(title) > 200:
        raise ValueError("That title is too long (200 characters at most).")
    return title, due, has_time
