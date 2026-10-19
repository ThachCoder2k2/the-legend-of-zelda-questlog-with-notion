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
- `make_sounds.py` → `sounds/*.wav`: original synthesized menu sounds (open, move, scroll, select,
  done, undo, cancel, track, untrack, close). Edit and re-run to change them; `"sound": ""` in the card JSON mutes the card.
  Your own files in `sounds/custom/` (same names, .wav/.aiff/.caf/.m4a/.mp3) replace the built-in ones.

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
   (07:30, 12:00, 22:00, Sunday 20:00) applies it through the Notion connector first thing.
2. **Instant (optional):** give the card its own Notion integration token.
   1. Notion → Settings → Connections → Develop or manage integrations → New integration
      (internal, your workspace, capabilities: Read + Update content). Copy the secret.
   2. Open the Quest Log page → ••• → Connections → add that integration.
   3. In Terminal: `security add-generic-password -s "QuestLog Notion" -a token -w`
      (it asks for the secret; paste it there, so it never lands in shell history).
   Remove it with `security delete-generic-password -s "QuestLog Notion" -a token`.
   If Notion cannot be reached, the click falls back to the queue.

## Card JSON

| field | type | notes |
|---|---|---|
| kind | "morning" \| "midday" \| "evening" \| "weekly" | colour theme and header icon |
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
