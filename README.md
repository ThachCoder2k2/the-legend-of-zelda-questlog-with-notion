# Quest Log cards

Pop-up report cards for the Notion board "Quest Log", used by the scheduled tasks
questlog-morning-report, questlog-evening-reminder (12:00), questlog-evening-report and
questlog-weekly-reset.

- `card.html`: the design (Zelda: Tears of the Kingdom-style menu card: Purah Pad black,
  cyan italic text, sliding gold cursor, stamina-wheel progress, item description pane; CSS/SVG only).
  Preview in a browser with `?kind=morning|midday|evening|weekly` to see sample data.
- `QuestCard.swift` → `questcard`: opens card.html in a floating panel (top-right of the screen
  under the mouse). Rebuild: `swiftc -O QuestCard.swift -o questcard -framework Cocoa -framework WebKit`
- `show_card.py`: `python3 show_card.py card.json` launches the card detached; falls back to
  a plain AppleScript dialog if `questcard` is missing.
- `pending.py`: queue of quest status changes clicked on the card (`pending.json`), applied by
  the next scheduled task (`list`, `done ID AT`, `applied`).
- `report.py`: builds the morning / midday / evening cards straight from the Notion API, without Claude
  (see "Local runner" below).
- `make_sounds.py` → `sounds/*.wav`: original synthesized menu sounds (open, move, scroll, select,
  done, undo, cancel, track, untrack, close, alert). Edit and re-run to change them; `"sound": ""` in the card JSON mutes the card.
  Your own files in `sounds/custom/` (same names, .wav/.aiff/.caf/.m4a/.mp3) replace the built-in ones.

## When the Mac was asleep, off or away (catch-up) and the button

- **Sleep:** launchd runs a job that was due during sleep as soon as the Mac wakes up (that is why 07:30 can show at 08:16).
  A wake-up on the lock screen (or a dark wake / Power Nap) is not you being there, so the card waits; `questbar` runs the
  catch-up again 5 s after a wake-up and after you unlock, so it appears at once instead of at the next 10-minute tick.
- **Powered off:** launchd skips those jobs, so `com.questlog.catchup` also runs at login and every 10 minutes. It shows
  the one card that is due and unseen, **only once you are back at the Mac** (keyboard or mouse used in the last 10 minutes)
  and **never on top of a card that is open**. A card only counts inside its window: morning 07:00–11:30, midday
  11:30–21:30, evening 21:30–04:00 (the evening report after midnight is still for the previous day). The windows tile the
  day, so a late start shows one card, never a stack and never two within minutes. On Sundays the evening window closes at
  22:30, when the weekly reset starts moving quests, so the report never shows numbers the reset already changed. The weekly
  reset has its own catch-up (the Monday–Saturday check in its prompt) and waits for the Sunday evening report (`gate weekly`).
- **Live vs shadow** (`python3 report.py status`): *shadow* (default) = the Claude tasks show the cards and the local runner
  only logs; *live* (`python3 report.py install --live`, needs the Notion token: `report.py check`) = the local runner
  shows them, even with the Claude app closed, and the Claude tasks stop themselves at their "late guard" step.
  Without the token the catch-up can't build a card, so it shows one small notice per day (any kind) and keeps retrying quietly.
- **Late guard:** the three daily Claude prompts start with `report.py gate KIND`: it says "skip" outside the window, when
  the card was already shown, or when the local runner is live; otherwise "go DATE".
- **The button:** `questbar` puts the game's gold quest marker in the menu bar (mint once today is cleared; the number is
  today's cleared/total, as of the last card shown, hover for the time). Click, or **⌃⌥Q** anywhere = open
  the card now (`report.py now`): live from Notion, or, when Notion can't be reached, the last saved card labelled
  "Saved 22:11". Right-click: Morning / Midday / Evening / Weekly (last one) / the board / Quit. Every card ever shown is
  saved in `runs/last-KIND.json`, so the button also works offline. A manual open never marks a slot as shown, except when it opens a card that was due and unseen:
  then that card counts as shown. `report.py KIND --today DATE` (a preview of another day) saves nothing.
- **Tracking timer:** the quest you hit Track on (🔥 Grinding) shows as "▶ 12m" next to the icon (the tooltip and the menu's top item
  name it; the menu item opens it in Notion). It reads this Mac's card clicks (`pending.json`, `applied.json`) and drops out when you
  clear, untrack or move the quest, or after 10 hours.
- **Missing or empty token:** while there is no usable token the icon carries a "⚠" and the menu starts with "⚠ Notion isn't connected:
  set it up…" (a paste that didn't take leaves an empty Keychain item, which counts as missing).
- **The "!" badge:** `runs/urgent.json` = `{"items": [{"title", "due" (ISO date-time or YYYY-MM-DD), "url"}]}` puts an
  orange "!" on the icon: amber under 24 h, orange under 3 h, red and blinking under 1 h or overdue (the ring turns coral);
  the right-click menu lists those quests with a countdown and opens them. Levels come from the clock, so they work offline.
  Filled from the Quests "Due" property (see "Deadlines" below).
- A saved card shown the next day relabels its days ("Yesterday · Mon 19/10", "Today · Tue 20/10") instead of saying "Tomorrow" for today.
- Files: `quickadd.py` (the "Add deadline…" line), `set-notion-token.command` (token setup), `urgent.py` (deadlines and their escalation ladder), `qlstate.py` (windows, stamps, claim locks, saved cards, mode), `QuestBar.swift` → `questbar`
  (`swiftc -O QuestBar.swift -o questbar -framework Cocoa -framework Carbon`), logs in `runs/`.
  `report.py install` loads: the three daily agents, `com.questlog.catchup`, `com.questlog.bar`; `uninstall` removes them.
  A true desktop widget (WidgetKit) would need a signed app extension built in Xcode; the menu-bar button covers the same use.

## Deadlines

Give a quest a **Due** (date + time) in Notion (a date without a time means 23:59). Open quests that have one appear in the
board view **⏰ Deadlines**, on the cards as a "Due Today 17:00" tag, on the menu-bar icon as the "!" badge, and they climb a ladder
of urgent cards (`urgent.py`):

| time left | what happens |
|---|---|
| 3 h | amber/orange badge, one silent compact card |
| 1 h | red blinking badge, card with the open sound |
| 15 min | card with the alert sound, again every 10 min until you act |
| overdue | card with the alert sound, again every 30 min (stops 3 days after the deadline) |

The urgent card is red, compact, with **Start** (T, tracks the quest), **Done** (D), **Snooze** (S, 15 minutes) and Details / Close.
Rules: each step shows once; a quest you track (🔥 Grinding) keeps the steps but loses the repeats; a quest you clear on a card
disappears at once; alerts wait until you are at the Mac, and only the 15-minute and overdue steps may replace a card that is open;
the routine time windows do not apply. Several deadlines in one step come as one card.

**Phone push (ntfy.sh, off until you enable it).** While you are away from the Mac (no input for 10 minutes, or the screen locked) and
the Mac is awake, the 1 h, 15 min and overdue steps go to your phone instead of waiting for a card: one notification per step
(overdue: again every hour, three times at most), never the 3 h heads-up, never while you are at the Mac, never while snoozed.
Setup: `python3 urgent.py push setup` makes `push.json` (private, not in git) with a random topic; install the ntfy app, subscribe to
that topic; `push sample` shows exactly what would be sent; `push enable | disable | status | test`. What leaves the Mac: the
notification (title "Quest Log · Last call", the quest title, its due time and countdown, a link to its Notion page) to
https://ntfy.sh, addressed to your topic. Anyone who knows the topic can read it, so treat it like a password. Set `"titles": false`
in push.json to send only "A deadline · Today 17:00 · in 14 min" with no title and no link. A sleeping Mac sends nothing.

**Add deadline…** (right-click the button, or **⌃⌥D** anywhere): type one line and it becomes a 🧩 Side Quest in Notion with its
Due, planned on the day it is due, and goes on the deadline list at once (`report.py add`, parsed by `quickadd.py`):
`Submit visa form, tomorrow 9am` · `Pay rent in 3h` · `Report, fri 17:00` · `Call the bank, 26/10 10:30` · `Renew passport 2026-11-05`
(a day without a time = until 23:59; a bare time = the next time the clock shows it; `17h` = 17:00, `3h` = in three hours).
It needs the token with Insert content. If the line can't be read, the box comes back with your text.

On the cards, **Move to tomorrow** says "· after its deadline" (and a toast) when the new day is later than the quest's Due.
The weekly reset never moves a quest past its Due (rule 3-0 in its prompt: deadline quests are placed first, the day before
the deadline when possible, and anything overdue or sharing a Main day is reported).

Where the list comes from (`runs/urgent.json`): `report.py refresh` (the catch-up agent runs it every 10 minutes, and every card the
local runner builds refreshes it) needs the Notion token; without the token the three daily Claude tasks fill it through
`urgent.py set` (step U of their prompts), so a deadline added at 14:00 is only noticed at the next daily card. `questbar` runs
`report.py urgent` every minute while a deadline is within 3 hours (the catch-up agent also does, every 10 minutes).
Commands: `python3 urgent.py list | tick [--force] [--dry-run] | snooze [MIN] | set (JSON on stdin) | clear`.
Preview the card: `card.html?kind=urgent`.

## Tests

`tests/run.sh` runs the whole suite (about 200 checks: time windows, catch-up, locks, deadlines, quick add, push, evening numbers…)
in a throw-away sandbox: a temp copy of the scripts, a fake `questcard` and `osascript`, the network stubbed. It touches no real
state and sends nothing to Notion or ntfy.sh. It needs only `/usr/bin/python3`.

## Full screen (default)

The card opens full screen like the Purah Pad menu: tabs on top, quest list on the left, the detail panel
(always shown, following the cursor) on the right, prompts bottom-right. **M** (or a click on the dimmed
backdrop) shrinks it to the corner card; M again goes back. `"display": "compact"` in the card JSON opens it
as the corner card instead.

## Controls (TOTK Adventure Log style)

| input | action |
|---|---|
| hover, ↑ / ↓ (or j / k) | move the gold cursor; the pane below (and the detail panel, if open) follows |
| click the slot at a quest's right end, T | **track** it (TOTK-style: one tracked quest at a time; tracked = 🔥 Grinding); again = untrack |
| Space, the status chip in the detail panel | action menu: Clear · Track · Move to tomorrow · To do · Undo · Open in Notion |
| double-click, Enter, → | detail panel beside the card (map preview, questline goal, next step, questline log) |
| Enter in the detail panel | open the quest in Notion |
| B | open the board |
| M | full screen ⇄ corner card |
| Esc | close the menu, then the detail panel, then the card |
| drag the header | move the card |

Each quest shows its questline's illustration (row tile + banner in the detail panel), drawn as original SVG in
`card.html` (`ART`): Unbound Pawn, Konosuba, Play Cyberpunk, Master's Decision, AI Fintech, Economics, then
Main / Side / Fun defaults. New questlines: add a banner, a tile and a name pattern to `ART`.

Keys work only after you click the card once, so typing in another app never reaches it.
"Move to tomorrow" sets the quest's Date to the next day and its Status to To Do.

## Saving "done" to Notion

The card has no Notion access of its own, so a click is saved one of two ways:

1. **Queued (default):** written to `pending.json` (status, plus date for a move); the next scheduled Quest Log task
   (07:30, 12:00, 22:00, Sunday 22:30) applies it through the Notion connector first thing.
2. **Instant (optional):** give the card its own Notion integration token.
   1. Notion → Settings → Connections → Develop or manage integrations → New integration
      (internal, your workspace, capabilities: Read, Update **and Insert** content; Insert is what "Add deadline…" needs). Copy the secret.
   2. Open the page **🗂️ Quest Log Data** → ••• → Connections → add that integration. That page holds the
      Quests and Questlines databases; it is a separate top-level page, so connecting only the Quest Log board
      page is not enough (every call would fail with 404).
   3. Right-click the menu-bar button, "⚠ Notion isn't connected: set it up…" (shown while the token is missing; it opens
      `set-notion-token.command` in Terminal), or run yourself `security add-generic-password -U -s "QuestLog Notion" -a token -w`
      (it asks for the secret; paste it there, so it never lands in shell history).
   Remove it with `security delete-generic-password -s "QuestLog Notion" -a token`.
   If Notion cannot be reached, the click falls back to the queue.
   Test it with `python3 report.py check`.

## Local runner (no Claude needed)

`report.py` makes the 07:30, 12:00 and 22:00 cards itself from the Notion API with the token above: a few
seconds, no Claude tokens, and it works with the Claude app closed. It follows the prompts of
questlog-morning-report, questlog-evening-reminder and questlog-evening-report (~/.claude/scheduled-tasks/…/SKILL.md)
step by step, including step 0 (queued clicks are written to Notion and recorded in applied.json through
`pending.py done`). The Sunday weekly reset stays a Claude task: it needs planning judgement.

| command | does |
|---|---|
| `python3 report.py morning` (or `midday`, `evening`) | apply queued clicks, read Notion, show the card |
| `… --dry-run` | print the card JSON only: no Notion writes, pending.json untouched, no card |
| `… --today 2026-10-21` | build the card for another day |
| `python3 report.py compare evening` | diff the Claude task's last card (`/tmp/questlog-evening-card.json`) against ours |
| `python3 report.py install` / `install --live` / `uninstall` | LaunchAgents at 07:30, 12:00, 22:00 (`~/Library/LaunchAgents/com.questlog.report.*.plist`) |
| `python3 report.py check` | token, Notion access, row counts |

**Shadow mode (installed now).** The LaunchAgents show nothing and write nothing to Notion. Each one waits for
the Claude task's card, builds ours at that moment and appends the result to `runs/compare.log`
(differences in `runs/DATE-KIND.diff.txt`). Once a few days read `MATCH`, go live (first `touch runs/DATE-morning.shown runs/DATE-midday.shown` for the cards of
today you already saw, or the catch-up shows them again):
run `python3 report.py install --live`, then turn off the three Claude tasks (morning, midday, evening) in the
Claude app so the card doesn't show twice. Undo: `python3 report.py install` (back to shadow) and turn them on again.

Live runs: a run that fires after a wake-up only shows when it still makes sense (morning 07:00–12:00,
midday 11:30–22:00, evening 21:30–04:00, after midnight as the previous day's report), each card once a day;
if Notion can't be read the card says why instead of staying silent. Cards and logs: `runs/` (kept 30 days).

Known differences from the Claude tasks:
- "Wrap shell commands in backticks" is judgement in the prompts; `report.py` wraps commands it recognises
  (git, npm, python3, head, … with their arguments, e.g. `git status --short | head -40`).
- The evening card follows the prompt's rules for the day's numbers: quests moved to tomorrow with the card stay in Today and in
  the X/Y count (tag "→ moved to Wed 21/10"), a Main cleared today counts once for the streak, never also for its own earlier day.
- The API returns 2-Min Start without the connector's Markdown escapes (`~31 min`, not `\~31 min`).
- 1 quest reads "1 quest to do" / "1 quest left" instead of "1 quests".
- The evening "cleared today" count also sees undated quests cleared today, which the connector's views can't show.
- If you edit a task prompt, `report.py` doesn't follow by itself: change it too (shadow mode will show the drift).

## Card JSON

| field | type | notes |
|---|---|---|
| kind | "morning" \| "midday" \| "evening" \| "weekly" \| "urgent" | colour theme and header icon (urgent = the deadline card: compact only, with Start / Done / Snooze) |
| badge | emoji | unused by the TOTK design (icon comes from kind) |
| eyebrow | string | small line above the title |
| title | string | |
| subtitle | string | optional |
| progress | {done, total} | optional stamina wheel; morning/evening count the first section live |
| stats | [{icon, value, label}] | optional chips |
| sections | [{heading, items: [item], empty}] | quest lists; `empty` = text when items is empty |
| item | see below | |
| xp | [{name, done, total}] | optional XP bars (weekly) |
| callout | {label, text} | 2-minute start for cards whose items have no `start` (older data) |
| note | string | optional small line at the bottom |
| url | string | board URL for "Board" (default: the Quest Log board) |
| display | "full" \| "compact" | how the card opens (default full) |
| timeoutSec | number | auto-close, default 1800 |

Item: `{type: "main"|"side"|"fun", title, label?, tags?: [string], live?, done?, url?, status?, date?, start?, questline?, goal?, energy?, steps?}`

- `label`: day chip like "Mon 26/10"; `live`: "in progress"; `done`: dimmed with a mint check.
- `url`: the quest's Notion page (`https://app.notion.com/p/<32 hex id>`). Without it the row
  can't be opened or changed.
- `status`: Notion Status exactly ("📜 To Do", "🔥 Grinding", "⏸ Delay", "✅ Cleared"); Undo restores it.
- `date`: the quest's Date, `YYYY-MM-DD`; needed for "Move to tomorrow" (and its undo).
- `start`: the quest's "2-Min Start" (wrap shell commands in backticks); shown as the next step.
- `questline`, `goal` (the questline's "Done means"), `energy` ("🐸 Mobs" / "🐉 Boss"): pane and detail panel.
- `steps`: the questline's quests `[{title, url?, date?, done?}]`, shown as the questline log.

All card text is English (day labels Mon–Sun).
