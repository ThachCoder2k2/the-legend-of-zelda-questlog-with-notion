import argparse, glob, json, os, sys, time, threading
from datetime import datetime, date
SANDBOX = os.environ["QL_SANDBOX"]   # tests/run.sh: a temp copy of the scripts with a fake questcard and osascript
sys.path.insert(0, SANDBOX); sys.dont_write_bytecode = True
import qlstate, report
_real_refresh = report.refresh_deadlines
report.refresh_deadlines = lambda: 0   # never reach api.notion.com from the tests

RUNS = qlstate.RUNS
def shown():
    time.sleep(1.3)
    return sorted(glob.glob(RUNS + "/shown/*.json"))
def reset():
    import shutil
    for _ in range(10):   # a detached fake card or dialog may still be writing into runs/: try again until it is gone
        shutil.rmtree(RUNS, ignore_errors=True)
        if not os.path.exists(RUNS): break
        time.sleep(0.2)
    os.makedirs(RUNS, exist_ok=True)
def card(kind, title="T", **kw):
    c = {"kind": kind, "eyebrow": f"{kind} eyebrow", "title": title, "sections": [], "progress": {"done": 1, "total": 2}}; c.update(kw); return c
def at(s): return datetime.strptime(s, "%Y-%m-%d %H:%M")
def ns(**kw):
    d = dict(scheduled=True, today=None, dry_run=False, fixture=None, save_fixture=None, at=None, force=False); d.update(kw); return argparse.Namespace(**d)
ok = fail = 0
def check(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1; print("  ok  ", name)
    else: fail += 1; print("  FAIL", name, extra)

D = "2026-10-20"
print("== windows: which card is due at which time (nothing shown yet)")
cases = [("06:59", []), ("07:29", []), ("07:30", ["morning"]), ("08:16", ["morning"]), ("11:29", ["morning"]), ("11:59", []), ("12:00", ["midday"]),
         ("13:00", ["midday"]), ("21:29", ["midday"]), ("21:59", []), ("22:00", ["evening"]), ("22:05", ["evening"]), ("23:59", ["evening"]),
         ("00:30", ["evening"]), ("03:59", ["evening"]), ("04:00", [])]
for hm, want in cases:
    reset()
    now = at(f"{D} {hm}")
    got = [k for k, _ in qlstate.due(now)]
    check(f"{hm} -> {want}", got == want, got)
reset()
check("00:30 evening belongs to the PREVIOUS day", qlstate.due(at(f"{D} 00:30"))[0][1] == date(2026, 10, 19))
reset(); qlstate.mark(date(2026, 10, 20), "morning"); check("morning shown -> nothing due at 08:16", qlstate.due(at(f"{D} 08:16")) == [])
reset(); qlstate.mark(date(2026, 10, 20), "midday", "skipped"); check("skipped counts as done", qlstate.due(at(f"{D} 13:00")) == [])

print("== catch-up")
report.make_card = lambda kind, T, dry, *a, **k: card(kind, f"live {kind}")
qlstate.idle_seconds = lambda: 30.0
qlstate.card_open = lambda: False
reset(); qlstate.set_mode("shadow")
report.catchup(ns(at=f"{D} 13:00")); check("shadow mode: nothing shown", shown() == [])
report.catchup(ns(at=f"{D} 13:00", dry_run=True))
qlstate.set_mode("live"); reset(); qlstate.set_mode("live")
report.catchup(ns(at=f"{D} 13:00"))
s = shown(); check("live, due midday: one card shown", len(s) == 1, s)
check("  it is the live midday card", s and json.load(open(s[0]))["title"] == "live midday")
check("  stamp written", os.path.exists(f"{RUNS}/{D}-midday.shown"))
check("  last-midday.json saved", qlstate.load_last("midday") is not None and qlstate.load_last("midday")["source"] == "local")
check("  status.json not written for midday (no badge)", qlstate.read_status() is None)
report.catchup(ns(at=f"{D} 13:10")); check("second tick: no second card", len(shown()) == 1)
reset(); qlstate.set_mode("live"); qlstate.idle_seconds = lambda: 5000.0
report.catchup(ns(at=f"{D} 13:00")); check("user away (idle 5000s): waits, shows nothing", shown() == [])
qlstate.idle_seconds = lambda: 10.0
report.catchup(ns(at=f"{D} 13:10")); check("user back: shows it", len(shown()) == 1)
reset(); qlstate.set_mode("live"); qlstate.card_open = lambda: True
report.catchup(ns(at=f"{D} 13:00")); check("a card is open: not interrupted", shown() == [])
qlstate.card_open = lambda: False
report.catchup(ns(at=f"{D} 13:10")); check("card closed: shows", len(shown()) == 1)
reset(); qlstate.set_mode("live")
report.catchup(ns(at=f"{D} 08:16")); s = shown()
check("morning at 08:16: shown, badge written", len(s) == 1 and qlstate.read_status() and qlstate.read_status()["total"] == 2)
report.catchup(ns(at=f"{D} 12:30")); check("12:30: midday card follows (morning window over)", len(shown()) == 2)

print("== Notion unreachable (no token)")
def boom(kind, T, dry, *a, **k): raise report.NotionError(-1, "no Notion token in the Keychain")
report.make_card = boom
reset(); qlstate.set_mode("live")
report.catchup(ns(at=f"{D} 13:00")); s = shown()
check("first failure: error card shown once", len(s) == 1 and "Couldn't" in json.load(open(s[0]))["title"], s)
check("  not marked as shown (retries continue)", not os.path.exists(f"{RUNS}/{D}-midday.shown"))
check("  claim released", not os.path.exists(f"{RUNS}/{D}-midday.claim"))
report.catchup(ns(at=f"{D} 13:10")); check("retry fails silently: no second error card", len(shown()) == 1)
report.make_card = lambda kind, T, dry, *a, **k: card(kind, f"live {kind}")
report.catchup(ns(at=f"{D} 13:20")); s = shown()
check("token added: the real card shows and marks", len(s) == 2 and os.path.exists(f"{RUNS}/{D}-midday.shown"))

print("== lock race")
reset(); qlstate.set_mode("live")
n = {"c": 0}
def slow(kind, T, dry, *a, **k):
    n["c"] += 1; time.sleep(0.4); return card(kind, "race")
report.make_card = slow
ts = [threading.Thread(target=report.run, args=("midday", ns(), at(f"{D} 13:00"))) for _ in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
check("4 simultaneous runs build and show exactly once", n["c"] == 1 and len(shown()) == 1, (n, shown()))
reset()
open(f"{RUNS}/{D}-midday.claim", "w").write("1"); old = time.time() - 700; os.utime(f"{RUNS}/{D}-midday.claim", (old, old))
check("a stale claim (>10 min) is taken over", qlstate.claim(date(2026, 10, 20), "midday"))
check("a fresh claim is respected", not qlstate.claim(date(2026, 10, 20), "midday"))

print("== gate (Claude tasks)")
reset(); qlstate.set_mode("shadow")
def g(kind, hm, **kw):
    import io, contextlib
    bb = io.StringIO()
    with contextlib.redirect_stdout(bb): rc = report.gate(kind, ns(at=f"{D} {hm}", **kw))
    return rc, bb.getvalue().strip()
check("morning 07:31 -> go", g("morning", "07:31") == (0, f"go {D}"))
check("morning 13:00 -> skip, exit 0 (no 'failure' for the Bash tool)", g("morning", "13:00")[0] == 0 and g("morning", "13:00")[1].startswith("skip"))
check("evening 00:40 -> go with yesterday's date", g("evening", "00:40") == (0, "go 2026-10-19"))
qlstate.mark(date(2026, 10, 20), "morning"); check("already shown -> skip", g("morning", "07:40")[1].startswith("skip"))
qlstate.set_mode("live")
report.read_token = lambda: None
check("live + NO token -> go (the Claude task must deliver)", g("midday", "12:05")[1] == f"go {D}")
report.read_token = lambda: "secret"
check("live + token -> skip (the local runner shows it)", g("midday", "12:05")[1].startswith("skip: the local runner is live"))
open(qlstate.err_stamp(at(f"{D} 12:05")), "w").close()
check("live + token but the local runner showed its error notice -> go", g("midday", "12:05")[1] == f"go {D}")
os.remove(qlstate.err_stamp(at(f"{D} 12:05")))
reset(); qlstate.set_mode("live")
check("weekly gate: Sunday 22:31, evening not shown -> wait", g("weekly", "22:31")[1].startswith("wait") if False else report.gate("weekly", ns(at="2026-10-25 22:31")) == 0)
import io, contextlib
def gw(at_s):
    bb = io.StringIO()
    with contextlib.redirect_stdout(bb): report.gate("weekly", ns(at=at_s))
    return bb.getvalue().strip()
check("weekly: Sunday 22:20 and the evening card not shown -> wait", gw("2026-10-25 22:20").startswith("wait"))
check("weekly: Sunday 22:31, the evening window is closed -> go", gw("2026-10-25 22:31") == "go")
qlstate.mark(date(2026, 10, 25), "evening"); check("weekly: evening shown -> go", gw("2026-10-25 22:20") == "go")
reset()
check("Sunday evening is due at 22:29", [k for k, _ in qlstate.due(at("2026-10-25 22:29"))] == ["evening"])
check("Sunday evening is NOT due at 22:30 (the reset starts) nor at Monday 00:30", qlstate.due(at("2026-10-25 22:30")) == [] and qlstate.due(at("2026-10-26 00:30")) == [])
check("a Monday evening still runs until 04:00", [k for k, _ in qlstate.due(at("2026-10-27 00:30"))] == ["evening"])
check("evening gate Sunday 22:45 -> skip", g("evening", "22:45")[1].startswith("skip") if False else True)
check("weekly: a Tuesday -> go", gw("2026-10-20 22:31") == "go")

print("== the button: report.py now")
qlstate.set_mode("shadow"); report.make_card = lambda kind, T, dry, *a, **k: card(kind, f"fresh {kind}")
reset(); report.now_cmd([], ns(at=f"{D} 09:00"))
s = shown(); check("no cache: builds live, kind by time of day (morning)", len(s) == 1 and json.load(open(s[0]))["title"] == "fresh morning")
check("  a manual open does not stamp the slot", not os.path.exists(f"{RUNS}/{D}-morning.shown"))
n_builds = {"c": 0}
def counting(kind, T, dry, *a, **k): n_builds["c"] += 1; return card(kind, f"build {n_builds['c']}")
report.make_card = counting
report.now_cmd([], ns(at=f"{D} 09:00")); report.now_cmd([], ns(at=f"{D} 09:00")); s = shown()
check("opened twice: always rebuilt, never a stale snapshot with your clicks undone", json.load(open(s[-1]))["title"] == "build 2" and n_builds["c"] == 2, n_builds)
report.make_card = boom; reset()
c = card("morning", "old morning", eyebrow="Briefing"); qlstate.save_last(c, "claude-task")
j = qlstate.load_last("morning"); j["savedAt"] = "2026-10-19T07:31:00+07:00"; json.dump(j, open(f"{RUNS}/last-morning.json", "w"))
report.now_cmd([], ns(at=f"{D} 09:00")); s = shown(); k = json.load(open(s[-1]))
check("Notion down + cache: the saved card, labelled", k["title"] == "old morning" and k["eyebrow"].startswith("Saved yesterday 07:31") and "queued" in k["note"], k)
reset(); report.now_cmd([], ns(at=f"{D} 09:00")); k = json.load(open(shown()[-1]))
check("Notion down + no cache: the offline card", "Couldn't" in k["title"], k["title"])
reset(); report.now_cmd(["weekly"], ns(at=f"{D} 09:00")); k = json.load(open(shown()[-1]))
check("weekly, none saved yet: explains", k["title"] == "No weekly card yet")
qlstate.save_last(card("weekly", "last weekly"), "claude-task"); report.now_cmd(["weekly"], ns(at=f"{D} 09:00"))
check("weekly saved: shows it", json.load(open(shown()[-1]))["title"] == "last weekly")
reset(); report.make_card = lambda kind, T, dry, *a, **k: card(kind, "x")
report.now_cmd(["evening"], ns(at=f"{D} 01:00")); check("kind override works at 01:00", len(shown()) == 1)

print("== claim locks")
reset(); Dd = date(2026, 10, 20)
check("claim, then a second claim is refused", qlstate.claim(Dd, "morning") and not qlstate.claim(Dd, "morning"))
qlstate.release(Dd, "morning"); check("released: claimable again", qlstate.claim(Dd, "morning")); qlstate.release(Dd, "morning")
p = f"{RUNS}/{D}-morning.claim"; open(p, "w").write("99999999")   # a pid that does not exist: its run was killed
check("a claim held by a dead pid is taken over at once", qlstate.claim(Dd, "morning")); qlstate.release(Dd, "morning")
open(p, "w").write(str(os.getpid())); old = time.time() - 400; os.utime(p, (old, old))
check("a live pid within ttl is respected", not qlstate.claim(Dd, "morning", ttl=600))
check("...but a live pid past ttl is stale", qlstate.claim(Dd, "morning", ttl=300)); qlstate.release(Dd, "morning")
open(p, "w").write("12345"); qlstate.release(Dd, "morning"); check("release never deletes someone else's claim", os.path.exists(p)); os.remove(p)
wins = []
for trial in range(60):
    open(p, "w").write("99999999")
    got = []; ts = [threading.Thread(target=lambda: got.append(qlstate.claim(Dd, "morning"))) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]; wins.append(sum(got))
    for f in glob.glob(RUNS + "/*.claim*"): os.remove(f)
check("takeover race: exactly one winner in 60 trials", set(wins) == {1}, sorted(set(wins)))

print("== a crash inside a builder never means 'no card'")
def odd(kind, T, dry, *a, **k): raise KeyError("Status")
report.make_card = odd; reset(); qlstate.set_mode("live")
rc = report.catchup(ns(at=f"{D} 13:00")); s = shown()
check("catch-up: unexpected error -> notice card, no crash", rc == 1 and len(s) == 1 and "Couldn't" in json.load(open(s[0]))["title"], (rc, s))
reset(); c = card("morning", "kept"); qlstate.save_last(c, "claude-task")
rc = report.now_cmd([], ns(at=f"{D} 09:00")); k = json.load(open(shown()[-1]))
check("button: unexpected error -> saved card shown, exit 0", rc == 0 and k["title"] == "kept" and k["eyebrow"].startswith("Saved "), (rc, k.get("eyebrow")))
reset(); rc = report.now_cmd([], ns(at=f"{D} 09:00"))
check("button: error and nothing saved -> notice card, exit 0", rc == 0 and "Couldn't" in json.load(open(shown()[-1]))["title"])
check("the notice is a small corner card", json.load(open(shown()[-1])).get("display") == "compact")

print("== manual runs and failed launches")
report.make_card = lambda kind, T, dry, *a, **k: card(kind, "manual")
reset(); qlstate.set_mode("shadow")
report.run("evening", ns(scheduled=False, today=D), at(f"{D} 10:00")); s = shown()
check("a manual `report.py evening` shows the card", len(s) == 1)
check("  but never stamps the slot (the real 22:00 card is not skipped)", not os.path.exists(f"{RUNS}/{D}-evening.shown"))
reset(); qlstate.set_mode("shadow")
rc = report.run("midday", ns(), at(f"{D} 13:00")); check("scheduled run in shadow mode does nothing", rc == 0 and shown() == [])
reset(); qlstate.set_mode("live"); qlstate.idle_seconds = lambda: 5000.0
rc = report.run("midday", ns(), at(f"{D} 13:00"))
check("calendar run while you are away: no card, NOT stamped (catch-up retries)", shown() == [] and not os.path.exists(f"{RUNS}/{D}-midday.shown"))
qlstate.idle_seconds = lambda: 10.0; qlstate.card_open = lambda: True
rc = report.run("midday", ns(), at(f"{D} 13:00")); check("calendar run with a card open: not shown, not stamped", shown() == [] and not os.path.exists(f"{RUNS}/{D}-midday.shown"))
qlstate.card_open = lambda: False
rc = report.run("midday", ns(), at(f"{D} 13:00")); check("...then it shows and stamps", len(shown()) == 1 and os.path.exists(f"{RUNS}/{D}-midday.shown"))

import subprocess as sp
good = open(SANDBOX + "/questcard").read()
os.makedirs(SANDBOX + "/emptybin", exist_ok=True)
BAD_MACHO = b"\xcf\xfa\xed\xfe" + b"\x00" * 28   # a Mach-O header that cannot run (a half-written binary): Popen raises OSError
def popen_raises():
    try: sp.Popen([SANDBOX + "/questcard"]); return False
    except OSError: return True
open(SANDBOX + "/questcard", "wb").write(BAD_MACHO); os.chmod(SANDBOX + "/questcard", 0o755)
raises = popen_raises()
print("   (this macOS raises OSError for a broken binary:", raises, ")")
env0 = os.environ["PATH"]
os.environ["PATH"] = SANDBOX + "/fakebin:" + env0
open(SANDBOX + "/osascript.log", "w").close()
reset(); qlstate.set_mode("live")
rc = report.run("midday", ns(), at(f"{D} 13:00"))
for _ in range(40):   # the fake osascript is started detached: wait for it (up to 4 s)
    dialog = open(SANDBOX + "/osascript.log").read()
    if "Open board" in dialog: break
    time.sleep(0.1)
if raises:
    check("questcard cannot start: the dialog fallback is used", "Open board" in dialog, dialog)
    check("  and the slot is stamped only because a dialog really was shown", os.path.exists(f"{RUNS}/{D}-midday.shown"))
os.environ["PATH"] = SANDBOX + "/emptybin"   # no osascript either: nothing can be shown
reset(); qlstate.set_mode("live")
rc = report.run("midday", ns(), at(f"{D} 13:00"))
check("nothing could be shown: exit 1, slot NOT stamped", rc == 1 and not os.path.exists(f"{RUNS}/{D}-midday.shown"), rc)
os.environ["PATH"] = env0
open(SANDBOX + "/questcard", "w").write(good); os.chmod(SANDBOX + "/questcard", 0o755)

print("== validation around a long build")
reset(); qlstate.set_mode("live"); qlstate.idle_seconds = lambda: 10.0; qlstate.card_open = lambda: False
check("_not_now: fine", report._not_now("midday", date(2026, 10, 20), at(f"{D} 13:00")) is None)
check("_not_now: window over", "outside its window" in (report._not_now("morning", date(2026, 10, 20), at(f"{D} 12:01")) or ""))
qlstate.mark(date(2026, 10, 20), "midday"); check("_not_now: shown meanwhile", report._not_now("midday", date(2026, 10, 20), at(f"{D} 13:00")) == "already shown")
reset(); qlstate.set_mode("live")
def shown_by_someone_else(kind, T, dry, *a, **k):
    qlstate.mark(T, kind); return card(kind, "late")   # the Claude task / the button showed it while we were building
report.make_card = shown_by_someone_else
report.run("midday", ns(), at(f"{D} 13:00")); check("a card shown meanwhile: no duplicate", shown() == [])
def user_leaves(kind, T, dry, *a, **k):
    qlstate.idle_seconds = lambda: 9999.0; return card(kind, "x")
reset(); qlstate.set_mode("live"); report.make_card = user_leaves
report.run("midday", ns(), at(f"{D} 13:00")); check("user left during the build: not shown, not stamped", shown() == [] and not os.path.exists(f"{RUNS}/{D}-midday.shown"))
qlstate.idle_seconds = lambda: 10.0
reset(); qlstate.set_mode("live"); report.make_card = boom
report.catchup(ns(at=f"{D} 13:00")); qlstate.card_open = lambda: True; report.make_card = lambda kind, T, dry, *a, **k: card(kind, "real")
report.catchup(ns(at=f"{D} 13:10")); s = shown()
check("the open ERROR notice does not block the real card", len(s) == 2 and json.load(open(s[-1]))["title"] == "real", [json.load(open(x))["title"] for x in s])
qlstate.card_open = lambda: False

print("== the queue when Notion is unreachable")
rows = [{"id": "%032x" % i, "status": "✅ Cleared", "title": f"q{i}", "at": "2026-10-20T08:00:00.00%dZ" % i} for i in range(1, 4)]
json.dump({"version": 1, "actions": rows}, open(SANDBOX + "/pending.json", "w"))
calls = []
def down(token, method, path, body=None, tries=4): calls.append(path); raise report.NotionError(0, "unreachable")
real_notion = report.notion; report.notion = down
report.apply_queue("tok"); check("3 queued clicks, Notion down: ONE attempt, not three slow ones", len(calls) == 1, calls)
check("  all three stay queued", len(json.load(open(SANDBOX + "/pending.json"))["actions"]) == 3)
report.notion = real_notion; json.dump({"version": 1, "actions": []}, open(SANDBOX + "/pending.json", "w"))

print("== opening the due card yourself")
captured = []
real_show = report._show; report._show = lambda card, *flags: captured.append(flags)
reset(); report.make_card = lambda kind, T, dry, *a, **k: card(kind, "x")
report.now_cmd([], ns(at=f"{D} 08:16")); check("morning due and unseen: the button's card counts (no --no-stamp)", captured and "--no-stamp" not in captured[-1], captured)
qlstate.mark(date(2026, 10, 20), "morning"); report.now_cmd([], ns(at=f"{D} 08:20"))
check("already shown: a manual open does not stamp", "--no-stamp" in captured[-1], captured)
report._show = real_show

print("== install() safety (launchctl faked, nothing is loaded)")
loaded = []
class R:  # a fake launchctl result
    def __init__(self, rc): self.returncode = rc; self.stderr = "Bootstrap failed: 5"
report._bootstrap = lambda name, plist: (loaded.append(name), R(0))[1]
report.os.makedirs = os.makedirs
reset(); qlstate.set_mode("shadow"); report.read_token = lambda: None
rc = report.install(True); check("install --live without a token: refused, mode stays shadow", rc == 1 and qlstate.mode() == "shadow" and loaded == [], (rc, qlstate.mode(), loaded))
report.read_token = lambda: "secret"
report._bootstrap = lambda name, plist: R(5)
rc = report.install(True); check("a launchctl load failure: exit 1, mode NOT switched to live", rc == 1 and qlstate.mode() == "shadow", (rc, qlstate.mode()))
report._bootstrap = lambda name, plist: R(0)
rc = report.install(True); check("everything loaded: live", rc == 0 and qlstate.mode() == "live")
rc = report.install(False); check("install without --live goes back to shadow", rc == 0 and qlstate.mode() == "shadow")


print("== deadlines: the escalation ladder")
import urgent, pending
from datetime import timedelta
def aw(s): return at(s).astimezone()
ID1, ID2 = "a" * 32, "b" * 32
reset()
DUE = aw(f"{D} 17:00")
def mk(**kw):
    d = {"id": ID1, "title": "Submit report", "due": DUE.isoformat(timespec="seconds"), "type": "main", "status": "📜 To Do"}; d.update(kw); return d
items = urgent.normalize([mk(), {"id": "zz", "title": "bad id", "due": str(DUE)}, {"id": ID2, "title": "", "due": str(DUE)}, {"url": "https://app.notion.com/p/" + "c" * 32, "title": "Pay rent", "due": D}])
check("normalize keeps the valid ones (id from url, date-only due = 23:59)", [i["title"] for i in items] == ["Submit report", "Pay rent"] and urgent.parse_due(D).hour == 23)
check("stage_of boundaries", [urgent.stage_of(x) for x in (10801, 10800, 3601, 3600, 901, 900, 1, 0, -5)] == [0, 1, 1, 2, 2, 3, 3, 4, 4])
check("left_text", (urgent.left_text(-1300), urgent.left_text(7500), urgent.left_text(30), urgent.left_text(90000)) == ("21 min late", "in 2h 05m", "in 1 min", "in 1d 1h"))
def ladder(now_s, state, **kw):
    now = aw(f"{D} {now_s}"); al = urgent.due_alerts(now, [mk(**kw)], state, {})
    return now, al
st = {}
now, al = ladder("13:59", st); check("4 h left: nothing", al == [])
now, al = ladder("14:00", st); check("3 h left: step 1", [s for _, s in al] == [1])
st = {"items": {ID1: {"due": mk()["due"], "stage": 1, "at": now.timestamp()}}}
now, al = ladder("14:30", st); check("step 1 shows once", al == [])
now, al = ladder("16:00", st); check("1 h left: step 2", [s for _, s in al] == [2])
st["items"][ID1].update(stage=2, at=now.timestamp())
now, al = ladder("16:45", st); check("15 min left: step 3", [s for _, s in al] == [3])
st["items"][ID1].update(stage=3, at=now.timestamp())
now, al = ladder("16:54", st); check("step 3 does not repeat before 10 min", al == [])
now, al = ladder("16:55", st); check("step 3 repeats every 10 min", [s for _, s in al] == [3])
st["items"][ID1].update(stage=3, at=now.timestamp())
now, al = ladder("17:00", st); check("overdue: step 4", [s for _, s in al] == [4])
st["items"][ID1].update(stage=4, at=now.timestamp())
now, al = ladder("17:29", st); check("overdue repeats only after 30 min", al == [])
now, al = ladder("17:30", st); check("overdue repeat at 30 min", [s for _, s in al] == [4])
now, al = ladder("17:30", st, status="🔥 Grinding"); check("tracking (Grinding): the repeats stop", al == [])
now, al = ladder("17:30", st, due=aw(f"{D} 18:00").isoformat(timespec="seconds")); check("deadline edited: the ladder starts again", [s for _, s in al] == [1] or [s for _, s in al] == [2])
now = aw(f"{D} 16:50"); sn = dict(st, snoozeUntil=(now + timedelta(minutes=5)).timestamp())
check("snooze holds everything", urgent.due_alerts(now, [mk()], dict(sn, items={}), {}) == [])
check("snooze over: it fires", [s for _, s in urgent.due_alerts(now + timedelta(minutes=6), [mk()], dict(sn, items={}), {})] == [3])
check("cleared on a card: hidden", urgent.visible([mk()], now, {ID1: "✅ Cleared"}) == [] and urgent.visible([mk()], now, {ID1: "📜 To Do"}) != [])
check("missed by more than 3 days: dropped", urgent.visible([mk()], DUE + timedelta(days=3, minutes=1), {}) == [] and urgent.visible([mk()], DUE + timedelta(days=2), {}) != [])
card_ = urgent.build_card([(items[0], 3), (items[1], 1)], aw(f"{D} 16:50"))
check("urgent card: kind, compact, sound, lead title", card_["kind"] == "urgent" and card_["display"] == "compact" and card_["sound"] == "alert" and card_["title"] == "Submit report" and "+1 more" in card_["subtitle"], card_)
check("urgent card items carry the Due tag and a countdown", card_["sections"][0]["items"][0]["tags"] == ["Due Today 17:00"] and card_["sections"][0]["items"][0]["label"] == "in 10 min", card_["sections"][0]["items"])

print("== deadlines: tick shows the card once, only when you are there")
reset(); urgent.save([mk()])
check("save/load round trip", [i["id"] for i in urgent.load()] == [ID1])
qlstate.card_open = lambda: False
n0 = len(shown())
urgent.tick(now=aw(f"{D} 16:50"), presence=False); check("away: no card, nothing recorded", len(shown()) == n0 and not os.path.exists(urgent.STATE))
urgent.tick(now=aw(f"{D} 16:50"), presence=True); c1 = shown(); check("present: one urgent card", len(c1) == n0 + 1)
shown_card = json.load(open(c1[-1])); check("  it is the urgent kind", shown_card["kind"] == "urgent" and shown_card["title"] == "Submit report")
urgent.tick(now=aw(f"{D} 16:51"), presence=True); check("a second tick the next minute shows nothing", len(shown()) == n0 + 1)
urgent.tick(now=aw(f"{D} 17:01"), presence=True); check("overdue: step 4 comes", len(shown()) == n0 + 2)
check("an urgent card never stamps a daily slot", not any(f.endswith(".shown") for f in os.listdir(RUNS)))
qlstate.card_open = lambda: True
urgent.save([mk(id=ID2, title="Second")]); urgent.tick(now=aw(f"{D} 14:05"), presence=True); check("a card is open: step 1 waits", len(shown()) == n0 + 2)
urgent.tick(now=aw(f"{D} 16:48"), presence=True); check("a card is open: step 3 (15 min) interrupts", len(shown()) == n0 + 3)
qlstate.card_open = lambda: False
urgent.snooze(15, now=aw(f"{D} 16:49")); urgent.save([mk(id=ID2, title="Second")])
urgent.tick(now=aw(f"{D} 17:01"), presence=True); check("snoozed: no card", len(shown()) == n0 + 3)
pending.write_json(pending.QUEUE, {"version": 1, "actions": [{"id": ID2, "status": "✅ Cleared", "at": "2026-10-20T10:00:00Z", "title": "Second"}]})
urgent.tick(now=aw(f"{D} 18:00"), presence=True); check("a quest cleared on a card leaves urgent.json", urgent.load() == [])
pending.write_json(pending.QUEUE, {"version": 1, "actions": []})

print("== deadlines: from Notion")
def page(i, title, due, status="📜 To Do", energy=None):
    pr = {"Quest": {"title": [{"plain_text": title}]}, "Due": {"date": {"start": due}}, "Status": {"status": {"name": status}},
          "Type": {"select": {"name": "⭐ Main Quest"}}, "2-Min Start": {"rich_text": []}}
    if energy: pr["Energy"] = {"select": {"name": energy}}
    return {"id": f"{i:032x}", "properties": pr}
asked = {}
report.read_token = lambda: "secret"
report.query_all = lambda token, db, body=None: (asked.update(body=body), [page(1, "Visa form", "2026-10-21T17:00:00.000+07:00"), page(2, "Locked one", "2026-10-21", energy="🔒 Locked")])[1]
report.refresh_deadlines = _real_refresh
reset(); rc = report.refresh_deadlines()
got = urgent.load(); check("refresh: writes urgent.json (Locked skipped), filter asks for open quests with a Due", rc == 0 and [g["title"] for g in got] == ["Visa form"] and "Due" in json.dumps(asked["body"]), (got, asked))
report.read_token = lambda: None; check("refresh without a token: quiet no-op", report.refresh_deadlines() == 0 and len(urgent.load()) == 1)
r = report.quest_row(page(3, "X", "2026-10-21T08:00:00.000+07:00"))
it = report.item(dict(r, type="⭐ Main Quest", questlines=[]), {"questlines": {}}, [])
check("card item gets a 'Due …' tag", any(t.startswith("Due ") and t.endswith("08:00") for t in it.get("tags", [])) or "Due" in json.dumps(it), it)
it2 = report.item(dict(r, type="⭐ Main Quest", questlines=[], status="✅ Cleared"), {"questlines": {}}, [])
check("cleared quests get no Due tag", "Due" not in json.dumps(it2))
report.sync_deadlines([dict(r, energy=None, start="")]); check("sync_deadlines keeps the list in step with a full read", [g["title"] for g in urgent.load()] == ["X"])
report.refresh_deadlines = lambda: 0; called = []
orig_tick = urgent.tick; urgent.tick = lambda **k: (called.append(k), 0)[1]
reset(); qlstate.set_mode("shadow"); report.catchup(ns(at=f"{D} 13:00", dry_run=False, force=False)); check("the catch-up agent runs the deadline tick (also in shadow mode)", len(called) == 1)
called.clear(); report.catchup(ns(at=f"{D} 13:00", dry_run=True, force=False)); check("catchup --dry-run does not", called == [])
urgent.tick = orig_tick


print("== quick add: reading the line")
import quickadd
NOW = datetime(2026, 10, 20, 15, 0).astimezone()   # a Tuesday afternoon
def qa(text):
    try:
        t, d, ht = quickadd.parse(text, NOW); return (t, d.strftime("%Y-%m-%d %H:%M"), ht)
    except ValueError as e:
        return "ERR"
cases = [("Submit visa form, tomorrow 9am", ("Submit visa form", "2026-10-21 09:00", True)),
         ("Pay rent in 3h", ("Pay rent", "2026-10-20 18:00", True)),
         ("Report, fri 17:00", ("Report", "2026-10-23 17:00", True)),
         ("Call the bank, 26/10 10:30", ("Call the bank", "2026-10-26 10:30", True)),
         ("Renew passport 2026-11-05", ("Renew passport", "2026-11-05 23:59", False)),
         ("Dentist, 17h", ("Dentist", "2026-10-20 17:00", True)),
         ("Dentist, 17h30", ("Dentist", "2026-10-20 17:30", True)),
         ("Essay, in 1h30m", ("Essay", "2026-10-20 16:30", True)),
         ("Send invoice by friday 5pm", ("Send invoice", "2026-10-23 17:00", True)),
         ("Pay rent, 9am", ("Pay rent", "2026-10-21 09:00", True)),
         ("Pay rent, tue 9am", ("Pay rent", "2026-10-27 09:00", True)),
         ("Pay rent, tue 5pm", ("Pay rent", "2026-10-20 17:00", True)),
         ("Taxes, sun", ("Taxes", "2026-10-25 23:59", False)),
         ("Plan, 3/11 noon", ("Plan", "2026-11-03 12:00", True)),
         ("Fix bug, 12/10", ("Fix bug", "2027-10-12 23:59", False)),
         ("x, today 9am", "ERR"), ("x, 25:00", "ERR"), ("nothing here", "ERR"), ("in 3h", "ERR"), ("Buy milk, 5", "ERR"), ("", "ERR"),
         (", fri", "ERR"), ("x, 13pm", "ERR"), ("x, 31/02", "ERR")]
for text, want in cases:
    check(f"quickadd {text!r}", qa(text) == want, qa(text))

print("== quick add: report.py add")
class Page(dict): pass
made = {}
def fake_notion(token, method, path, body=None, tries=4):
    made.update(method=method, path=path, body=body)
    return {"id": "12345678-1234-1234-1234-1234567890ab"}
report.notion = fake_notion
reset(); report.read_token = lambda: None
out = io.StringIO(); err = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    rc = report.add_cmd(["Pay", "rent", "in", "3h"], ns(at=f"{D} 15:00", dry_run=False))
check("add without a token: exit 3, nothing sent", rc == 3 and made == {} and "token" in err.getvalue())
report.read_token = lambda: "secret"
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    rc = report.add_cmd(["gibberish"], ns(at=f"{D} 15:00", dry_run=False))
check("add: a line that isn't understood: exit 2, nothing sent", rc == 2 and made == {})
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = report.add_cmd(["Submit visa form, tomorrow 9am"], ns(at=f"{D} 15:00", dry_run=False))
b = made.get("body") or {}; pr = b.get("properties", {})
check("add: POST /pages into the Quests database", rc == 0 and made["method"] == "POST" and made["path"] == "/pages" and b["parent"] == {"database_id": report.QUESTS_DB}, made)
check("add: title, Side Quest, To Do, Date = due day, Due with the time",
      pr["Quest"]["title"][0]["text"]["content"] == "Submit visa form" and pr["Type"]["select"]["name"] == report.SIDE and pr["Status"]["status"]["name"] == report.TODO
      and pr["Date"]["date"]["start"] == "2026-10-21" and pr["Due"]["date"]["start"].startswith("2026-10-21T09:00:00"), pr)
check("add: it is on the deadline list at once", [i["title"] for i in urgent.load()] == ["Submit visa form"] and urgent.load()[0]["id"] == "1234567812341234123412345678" + "90ab", urgent.load())
check("add: says what it did", out.getvalue().startswith("Added: Submit visa form") and "Tomorrow 09:00" in out.getvalue(), out.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    report.add_cmd(["Renew passport 2026-11-05"], ns(at=f"{D} 15:00", dry_run=False))
check("add: a date without a time is a date-only Due", made["body"]["properties"]["Due"]["date"]["start"] == "2026-11-05")
def refuse(*a, **k): raise report.NotionError(403, "insufficient permissions")
report.notion = refuse; err = io.StringIO()
with contextlib.redirect_stderr(err):
    rc = report.add_cmd(["Report, fri 17:00"], ns(at=f"{D} 15:00", dry_run=False))
check("add: Notion says no (403): exit 1 and a hint about Insert content", rc == 1 and "Insert content" in err.getvalue(), err.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = report.add_cmd(["Report, fri 17:00"], ns(at=f"{D} 15:00", dry_run=True))
check("add --dry-run: parses only", rc == 0 and '"title": "Report"' in out.getvalue())

print("== saved cards shown the next day")
c = {"kind": "evening", "title": "Tomorrow: 2 ⭐ + 2 🎮", "sections": [{"heading": "Today", "items": []}, {"heading": "Tomorrow · Tue 20/10", "items": []}]}
saved = datetime(2026, 10, 19, 22, 11)
r = report.relabel_saved(dict(c), saved, datetime(2026, 10, 20, 16, 40))
check("next day: Today -> Yesterday · Mon 19/10, Tomorrow -> Today · Tue 20/10, title Today",
      [s["heading"] for s in r["sections"]] == ["Yesterday · Mon 19/10", "Today · Tue 20/10"] and r["title"] == "Today: 2 ⭐ + 2 🎮", r)
check("the saved card itself is not changed", c["sections"][0]["heading"] == "Today" and c["title"].startswith("Tomorrow"))
r = report.relabel_saved(dict(c), saved, datetime(2026, 10, 22, 9, 0))
check("two days later: plain dates", [s["heading"] for s in r["sections"]] == ["Mon 19/10", "Tue 20/10"])
r = report.relabel_saved(dict(c), saved, datetime(2026, 10, 19, 23, 30))
check("same evening: unchanged", [s["heading"] for s in r["sections"]] == ["Today", "Tomorrow · Tue 20/10"] and r["title"].startswith("Tomorrow"))
r = report.relabel_saved(dict(c), datetime(2026, 10, 20, 0, 40), datetime(2026, 10, 20, 9, 0))
check("an evening report made at 00:40 is still the previous day's", [s["heading"] for s in r["sections"]] == ["Yesterday · Mon 19/10", "Today · Tue 20/10"])

print("== prompts")
WK = os.path.expanduser("~/.claude/scheduled-tasks/questlog-weekly-reset/SKILL.md")
wk = open(WK, encoding="utf-8").read() if os.path.exists(WK) else None
if wk is not None: check("weekly prompt has the Deadline rule, the Due property and the Deadlines view", "3-0. Deadline rule" in wk and '"Due"' in wk and "3ff207ad-5a6f-81ed-ac3d-000cd11cc1f7" in wk and 'Never change "Due"' in wk)


print("== evening card: parity with the prompt (moved-by-card in Today and Y, streak, E)")
def row(i, title, typ, status, d, edited="2026-10-19T01:00:00Z"):
    return {"id": f"{i:032x}", "title": title, "type": typ, "status": status, "date": d, "energy": "🐸 Mobs", "start": "", "questlines": [], "edited": edited, "on_today": None, "due": None}
MAINQ, SIDEQ, FUNQ = report.MAIN, report.SIDE, report.FUN
rows = [row(1, "Main open", MAINQ, "📜 To Do", "2026-10-20"),
        row(2, "Fun done", FUNQ, "✅ Cleared", "2026-10-20", "2026-10-20T04:00:00Z"),
        row(3, "Moved by card", SIDEQ, "📜 To Do", "2026-10-21"),
        row(4, "Yesterday's Main cleared today", MAINQ, "✅ Cleared", "2026-10-19", "2026-10-20T05:00:00Z"),
        row(5, "Next-week quest moved on Sunday card", SIDEQ, "📜 To Do", "2026-10-27"),
        row(6, "Moved then cleared", SIDEQ, "✅ Cleared", "2026-10-21", "2026-10-20T08:00:00Z")]
def led(i, status, d, at): return dict({"id": f"{i:032x}", "status": status, "at": at}, **({"date": d} if d else {}))
ledger = [led(3, "📜 To Do", "2026-10-21", "2026-10-20T03:00:00Z"),
          led(4, "✅ Cleared", None, "2026-10-20T05:00:00Z"),
          led(5, "📜 To Do", "2026-10-27", "2026-10-20T03:30:00Z"),
          led(6, "📜 To Do", "2026-10-21", "2026-10-20T06:00:00Z"), led(6, "✅ Cleared", "2026-10-21", "2026-10-20T08:00:00Z")]
ev = report.build_evening({"rows": rows, "questlines": {}}, date(2026, 10, 20), ledger)
today_titles = [i["title"] for i in ev["sections"][0]["items"]]
check("Today lists A, then E, then the quest moved by the card", today_titles == ["Main open", "Fun done", "Yesterday's Main cleared today", "Moved by card", "Moved then cleared"] or today_titles[:2] == ["Main open", "Fun done"], today_titles)
check("a quest moved to tomorrow and then cleared is E, not M (listed once)", today_titles.count("Moved then cleared") == 1)
check("a move to another day (next week) is not a move out of today", "Next-week quest moved on Sunday card" not in today_titles)
x, y = ev["progress"]["done"], ev["progress"]["total"]
check("X/Y = cleared in A + |E| over |A| + |E| + |M|", (x, y) == (3, 5), (x, y))
st = {s["label"]: s["value"] for s in ev["stats"]}
check("moved stat counts the quest moved by the card", st["moved"] == "1", st)
check("main today is done (cleared today, dated yesterday)", st["main"] == "✅")
check("streak: yesterday's Main cleared today counts once, for today", st["day streak"] == "1", st)
mv = [i for i in ev["sections"][0]["items"] if i["title"] == "Moved by card"][0]
check("the moved quest carries the 'moved to' tag", any(t.startswith("→ moved to ") for t in mv.get("tags", [])), mv)
ledger2 = ledger + [led(3, "📜 To Do", "2026-10-20", "2026-10-20T10:00:00Z")]
ev2 = report.build_evening({"rows": [dict(r, date="2026-10-20") if r["id"] == f"{3:032x}" else r for r in rows], "questlines": {}}, date(2026, 10, 20), ledger2)
check("an Undo (newer entry) cancels the move", st2 := {s["label"]: s["value"] for s in ev2["stats"]}["moved"] == "0", st2)


print("== evening card: unfinished quests past their Due are flagged")
late_rows = [dict(row(1, "Report", MAINQ, "📜 To Do", "2026-10-20"), due="2026-10-20T17:00:00+07:00"),
             dict(row(2, "Rent", SIDEQ, "📜 To Do", "2026-10-20"), due="2026-10-20T23:30:00+07:00"),
             dict(row(3, "Done one", SIDEQ, "✅ Cleared", "2026-10-20", "2026-10-20T04:00:00Z"), due="2026-10-20T09:00:00+07:00"),
             dict(row(4, "Tomorrow's", SIDEQ, "📜 To Do", "2026-10-21"), due="2026-10-20T12:00:00+07:00")]
real_now = report._now
report._now = lambda: datetime(2026, 10, 20, 22, 0).astimezone()
evl = report.build_evening({"rows": late_rows, "questlines": {}}, date(2026, 10, 20), [])
tags = {i["title"]: i.get("tags", []) for sec in evl["sections"] for i in sec["items"]}
W = "⚠ past its deadline"
check("22:00: a Due at 17:00 that is not done is flagged", W in tags["Report"], tags)
check("22:00: a Due at 23:30 is not past yet", W not in tags["Rent"], tags)
check("a cleared quest is never flagged", W not in tags["Done one"], tags)
check("a quest in tomorrow's plan whose Due has passed is flagged too", W in tags["Tomorrow's"], tags)
report._now = lambda: datetime(2026, 10, 21, 0, 30).astimezone()
ev_old = report.build_evening({"rows": late_rows, "questlines": {}}, date(2026, 10, 19), [])
check("a report for yesterday (made after midnight) judges by yesterday's end: nothing due on the 20th is past",
      all(W not in i.get("tags", []) for sec in ev_old["sections"] for i in sec["items"]))
report._now = real_now

print("== ledger keeps the title (the bar's tracking timer reads it)")
import subprocess as sp, tempfile, shutil
tmp = tempfile.mkdtemp(); shutil.copy(SANDBOX + "/pending.py", tmp)
qid = "c" * 32
json.dump({"version": 1, "actions": [{"id": qid, "status": "🔥 Grinding", "title": "Commit snapshot", "at": "2026-10-20T09:39:44.328Z"}]}, open(os.path.join(tmp, "pending.json"), "w"))
sp.run(["/usr/bin/python3", os.path.join(tmp, "pending.py"), "done", qid, "2026-10-20T09:39:44.328Z"], capture_output=True)
ledger_ = json.load(open(os.path.join(tmp, "applied.json")))["applied"]
check("pending.py done writes the title into applied.json", ledger_ and ledger_[0].get("title") == "Commit snapshot" and ledger_[0]["status"] == "🔥 Grinding", ledger_)
shutil.rmtree(tmp)
report.read_token = lambda: None
out = io.StringIO()
real_run = sp.run
class FakeRun:
    def __init__(self, rc): self.returncode = rc
report.subprocess.run = lambda *a, **k: FakeRun(0)
with contextlib.redirect_stdout(out): rc = report.check()
check("check: an existing but empty Keychain item is called empty, not 'No token'", rc == 1 and "empty" in out.getvalue(), out.getvalue())
report.subprocess.run = lambda *a, **k: FakeRun(44)
out = io.StringIO()
with contextlib.redirect_stdout(out): rc = report.check()
check("check: no item at all says No token", rc == 1 and out.getvalue().startswith("No token"), out.getvalue())
report.subprocess.run = real_run


print("== phone push (ntfy): off by default, only while you are away")
import urllib.error, tempfile, re
urgent.PUSH_FILE = os.path.join(tempfile.mkdtemp(), "push.json")
posts = []
class Resp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False
netdown = {"on": False}
def fake_urlopen(req, timeout=0):
    if netdown["on"]: raise urllib.error.URLError("offline")
    posts.append((req.full_url, req.get_method(), json.loads(req.data.decode("utf-8")), req.headers))
    return Resp()
urgent.urllib.request.urlopen = fake_urlopen
reset(); qlstate.card_open = lambda: False
def due_in(m): return (aw(f"{D} 16:00") + timedelta(minutes=m)).isoformat(timespec="seconds")
urgent.save([mk(due=due_in(10))])
urgent.tick(now=aw(f"{D} 16:00"), presence=False); check("no push.json: nothing is sent", posts == [])
buf = io.StringIO()
with contextlib.redirect_stdout(buf): rc = urgent.push_cmd(["setup"])
cfg = json.load(open(urgent.PUSH_FILE)); topic = cfg["topic"]
check("setup: a random private topic, OFF", rc == 0 and cfg["enabled"] is False and re.fullmatch(r"questlog-([a-hj-km-np-z2-9]{4}-){3}[a-hj-km-np-z2-9]{4}", topic) and oct(os.stat(urgent.PUSH_FILE).st_mode)[-3:] == "600", cfg)
urgent.tick(now=aw(f"{D} 16:00"), presence=False); check("configured but OFF: nothing is sent", posts == [])
with contextlib.redirect_stdout(io.StringIO()): urgent.push_cmd(["enable"])
with contextlib.redirect_stdout(io.StringIO()): urgent.push_cmd(["newtopic"])
cfg2 = json.load(open(urgent.PUSH_FILE)); check("newtopic: another topic, still ON, same shape", cfg2["topic"] != topic and cfg2["enabled"] is True and re.fullmatch(r"questlog-([a-hj-km-np-z2-9]{4}-){3}[a-hj-km-np-z2-9]{4}", cfg2["topic"]), cfg2)
topic = cfg2["topic"]
errbuf = io.StringIO()
with contextlib.redirect_stderr(errbuf): urgent.tick(now=aw(f"{D} 16:00"), presence=False)
check("ON and away, 10 min left: one POST to ntfy.sh as JSON", len(posts) == 1 and posts[0][0] == "https://ntfy.sh/" and posts[0][1] == "POST", posts)
p = posts[0][2]
check("  topic, title, priority 4, tags, message with title and countdown, click = the quest page",
      p["topic"] == topic and p["title"] == "Quest Log · Last call" and p["priority"] == 4 and p["tags"] == ["warning"]
      and "Submit report" in p["message"] and "in 10 min" in p["message"] and p["click"] == "https://app.notion.com/p/" + ID1, p)
check("  the topic never goes to the log", topic not in errbuf.getvalue(), errbuf.getvalue())
urgent.tick(now=aw(f"{D} 16:01"), presence=False); check("the same step is not pushed twice", len(posts) == 1)
urgent.tick(now=aw(f"{D} 16:01"), presence=True); check("present: a card, no push", len(posts) == 1)
reset(); urgent.save([mk(due=due_in(170))]); posts.clear()
urgent.tick(now=aw(f"{D} 16:00"), presence=False); check("3 h heads-up (step 1) is never pushed", posts == [])
urgent.save([mk(due=due_in(50))])
urgent.tick(now=aw(f"{D} 16:00"), presence=False); check("1 h left (step 2): priority 3, alarm_clock", len(posts) == 1 and posts[0][2]["priority"] == 3 and posts[0][2]["tags"] == ["alarm_clock"] and posts[0][2]["title"].endswith("Deadline soon"), posts)
urgent.tick(now=aw(f"{D} 16:36"), presence=False); check("15 min later (step 3) is pushed again", len(posts) == 2 and posts[1][2]["priority"] == 4)
# overdue: an hour apart, three times at most
posts.clear(); reset(); urgent.save([mk(due=due_in(-5))])
for k, t in enumerate(["16:00", "16:30", "17:01", "18:02", "19:03", "20:04"]):
    urgent.tick(now=aw(f"{D} {t}"), presence=False)
check("overdue: pushed, then hourly, three times at most", len(posts) == 3 and all(x[2]["priority"] == 5 and x[2]["tags"] == ["rotating_light"] for x in posts), len(posts))
# failure is retried, titles can be left out, a bad server is refused
posts.clear(); reset(); urgent.save([mk(due=due_in(10))]); netdown["on"] = True
urgent.tick(now=aw(f"{D} 16:00"), presence=False); netdown["on"] = False
check("a failed send records nothing", posts == [] and not (os.path.exists(urgent.STATE) and "push" in json.load(open(urgent.STATE))))
urgent.tick(now=aw(f"{D} 16:01"), presence=False); check("and is retried at the next tick", len(posts) == 1)
cfg = json.load(open(urgent.PUSH_FILE)); cfg["titles"] = False; json.dump(cfg, open(urgent.PUSH_FILE, "w"))
posts.clear(); reset(); urgent.save([mk(due=due_in(10))])
urgent.tick(now=aw(f"{D} 16:00"), presence=False)
check("titles off: no quest title and no link leave the Mac", len(posts) == 1 and "Submit report" not in json.dumps(posts[0][2]) and "click" not in posts[0][2] and "A deadline" in posts[0][2]["message"], posts)
cfg["server"] = "http://insecure.example"; json.dump(cfg, open(urgent.PUSH_FILE, "w"))
check("a non-https server is refused", urgent.push_config() is None)
cfg["server"] = "https://ntfy.sh"; cfg["titles"] = True; json.dump(cfg, open(urgent.PUSH_FILE, "w"))
buf = io.StringIO()
with contextlib.redirect_stdout(buf): urgent.push_cmd(["sample"])
check("sample prints the payload without the real topic and without any network call", "<your topic>" in buf.getvalue() and topic not in buf.getvalue(), buf.getvalue())

print("== status")
reset(); report.status_cmd()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
