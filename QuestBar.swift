// QuestBar: the Quest Log button in the menu bar, the "widget" that opens the card any time.
//
//   click          open the Quest Log card for the time of day (live from Notion, or the last saved card offline)
//   right-click    menu: urgent deadlines, Add deadline…, Morning / Midday / Evening / Weekly, the Notion board, Quit
//   ⌃⌥Q            the same as a click, from anywhere
//   status actions the menu lists today's open quests (from runs/today.json, written whenever a card is shown); each has
//                  Clear / Track / Move to tomorrow / Open. The change is queued in pending.json like a card click, and
//                  `report.py apply` writes it to Notion right away when the token is saved.
//   tracking       a quest you hit Track on (🔥 Grinding, from pending.json / applied.json) shows "▶ 12m" next to the icon
//   ⌃⌥D            Add deadline…: one line ("Submit visa form, tomorrow 9am") becomes a quest with a Due in Notion
//
// The badge next to the icon is today's cleared/total (runs/status.json, written whenever a morning or evening card
// is shown). The app does no networking: every click runs `report.py now`, which does the work.
// Build: swiftc -O QuestBar.swift -o questbar -framework Cocoa -framework Carbon
// Started at login by the com.questlog.bar LaunchAgent (report.py install).

import Carbon.HIToolbox
import Cocoa

let boardURL = "notion://www.notion.so/3fe207ad5a6f81858d5fcde429bff7cf"
let python = "/usr/bin/python3"
let folder = (Bundle.main.executableURL ?? URL(fileURLWithPath: CommandLine.arguments[0]))
    .resolvingSymlinksInPath().deletingLastPathComponent()
let reportPy = folder.appendingPathComponent("report.py").path
let statusFile = folder.appendingPathComponent("runs/status.json")
let todayFile = folder.appendingPathComponent("runs/today.json")
let queueFile = folder.appendingPathComponent("pending.json")
let urgentFile = folder.appendingPathComponent("runs/urgent.json")
let logFile = folder.appendingPathComponent("runs/bar.log")

var hotKeyActions: [UInt32: () -> Void] = [:]   // by EventHotKeyID.id
let setupScript = folder.appendingPathComponent("set-notion-token.command")

/// A quest with a deadline (runs/urgent.json: {"items": [{"title", "due", "url"}]}; due = ISO-8601 with time, or a plain
/// date meaning the end of that day). How close it is decides the colour of the "!" badge on the icon.
struct Urgent {
    let title: String
    let due: Date
    let url: String?

    /// 0 = more than a day away (no badge), 1 = within 24 h, 2 = within 3 h, 3 = within 1 h or overdue.
    func level(now: Date = Date()) -> Int {
        let left = due.timeIntervalSince(now)
        if left <= 3600 { return 3 }
        if left <= 3 * 3600 { return 2 }
        if left <= 24 * 3600 { return 1 }
        return 0
    }

    func countdown(now: Date = Date()) -> String {
        let left = due.timeIntervalSince(now)
        let m = Int((abs(left) / 60).rounded())
        let text = m >= 1440 ? "\(m / 1440)d \((m % 1440) / 60)h" : m >= 60 ? "\(m / 60)h \(m % 60)m" : "\(max(m, 1))m"
        return left < 0 ? "overdue by \(text)" : "due in \(text)"
    }
}

func parseDue(_ raw: String) -> Date? {
    let iso = ISO8601DateFormatter()
    if let d = iso.date(from: raw) { return d }
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = iso.date(from: raw) { return d }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.calendar = Calendar(identifier: .gregorian)
    f.dateFormat = "yyyy-MM-dd"
    guard let day = f.date(from: String(raw.prefix(10))) else { return nil }
    return Calendar(identifier: .gregorian).date(bySettingHour: 23, minute: 59, second: 0, of: day)
}

/// The quest you are tracking right now: the newest card click (queued or written) of each quest, if it is "Track" and
/// recent. Only this Mac's clicks are known; a quest tracked elsewhere shows up once a card is shown.
struct Tracking {
    let id: String
    let title: String
    let since: Date

    var minutes: Int { max(0, Int(Date().timeIntervalSince(since) / 60)) }
    var elapsed: String { minutes < 60 ? "\(minutes)m" : "\(minutes / 60)h\(String(format: "%02d", minutes % 60))" }
}

/// The newest card click of each quest (queued in pending.json, or already written: applied.json), plus the titles seen.
struct Click {
    let at: Date
    let status: String
    let date: String?
}

func latestClicks() -> (clicks: [String: Click], titles: [String: String]) {
    var newest: [String: Click] = [:]
    var titles: [String: String] = [:]
    for (file, key) in [("applied.json", "applied"), ("pending.json", "actions")] {
        guard let data = try? Data(contentsOf: folder.appendingPathComponent(file)),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = obj[key] as? [[String: Any]] else { continue }
        for e in list {
            guard let id = e["id"] as? String, let status = e["status"] as? String,
                  let raw = e["at"] as? String, let at = parseDue(raw) else { continue }
            if let t = e["title"] as? String, !t.isEmpty { titles[id] = t }
            if newest[id] == nil || at > newest[id]!.at { newest[id] = Click(at: at, status: status, date: e["date"] as? String) }
        }
    }
    return (newest, titles)
}

func loadTracking() -> Tracking? {
    let (newest, titles) = latestClicks()
    let live = newest.filter { $0.value.status == "🔥 Grinding" && $0.value.at.timeIntervalSinceNow > -10 * 3600 }
    guard let top = live.max(by: { $0.value.at < $1.value.at }) else { return nil }
    return Tracking(id: top.key, title: titles[top.key] ?? "a quest", since: top.value.at)
}

/// One open quest of today, as the last shown card listed it, with your newer clicks applied.
struct TodayItem {
    let id: String
    let title: String
    let type: String
    let status: String
    let date: String?
}

func loadToday(todayKey: String) -> [TodayItem] {
    guard let data = try? Data(contentsOf: todayFile),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let days = obj["days"] as? [String: Any],
          let day = days[todayKey] as? [String: Any],
          let list = day["items"] as? [[String: Any]] else { return [] }
    let (clicks, _) = latestClicks()
    var out: [TodayItem] = []
    for e in list {
        guard let id = e["id"] as? String, let title = e["title"] as? String else { continue }
        var status = e["status"] as? String ?? "📜 To Do"
        var date = e["date"] as? String
        if let c = clicks[id] {   // a newer click wins over what the card said
            status = c.status
            if let d = c.date { date = d }
        }
        if status == "✅ Cleared" { continue }
        if let d = date, d > todayKey { continue }   // moved to a later day
        out.append(TodayItem(id: id, title: title, type: e["type"] as? String ?? "side", status: status, date: date))
    }
    return out
}

/// What a status action in the menu does: the new Status (and, for a move, the new Date) of one quest.
final class QuestAction: NSObject {
    let id: String
    let title: String
    let status: String
    let date: String?
    let flash: String
    init(id: String, title: String, status: String, date: String? = nil, flash: String) {
        self.id = id; self.title = title; self.status = status; self.date = date; self.flash = flash
    }
}

final class Bar: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var running: [Process] = []
    var lastCatchUp = Date.distantPast
    var lastUrgentTick = Date.distantPast
    var notionOK = true            // the Notion token is in the Keychain (checked at most once a minute)
    var lastTokenCheck = Date.distantPast
    var flash: (text: String, until: Date)?   // a short confirmation next to the icon ("✓ added")
    var addHotKey: EventHotKeyRef?
    var addOpen = false            // the Add deadline box is on screen
    var tracking: Tracking?        // the quest being tracked, if any
    var todayItems: [TodayItem] = []   // today's open quests (menu)
    var retryText = ""              // what was typed when the line wasn't understood: put back in the box
    var tooltipNote = ""            // what the last add said, until the next refresh of the tooltip
    var lastOpen = Date.distantPast
    var urgent: [Urgent] = []      // soonest deadline first
    var pulse = true               // the red "!" blinks
    var pulseTimer: Timer?
    var lastIconKey = ""

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.image = icon(done: false, level: 0, pulse: true)
            b.imagePosition = .imageLeft
            b.target = self
            b.action = #selector(clicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
            b.toolTip = "Quest Log: click to open · ⌃⌥Q"
        }
        buildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            self?.refresh()
            self?.urgentTick()
        }
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { [weak self] _ in
            guard let self = self, (self.urgent.first?.level() ?? 0) >= 3 else { return }
            self.pulse.toggle()
            self.refresh()
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.refresh()
            self?.catchUp()
        }
        // A card due while the screen was locked waits for you; show it the moment you unlock instead of at the next tick.
        DistributedNotificationCenter.default().addObserver(forName: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            self?.catchUp()
        }
        registerHotKey()
    }

    /// The game's quest marker: the gold tracking ring with its dot and a soft glow, in full colour (a template image
    /// would be one flat colour). Gold normally, mint once today is cleared (like the card's check), coral when a
    /// deadline is under an hour away or passed. `level` > 0 adds the game's orange "!" badge: amber within 24 h,
    /// orange within 3 h, red within 1 h or overdue (it blinks via `pulse`).
    func icon(done: Bool, level: Int, pulse: Bool) -> NSImage {
        let gold = NSColor(srgbRed: 1.0, green: 0.84, blue: 0.29, alpha: 1)
        let mint = NSColor(srgbRed: 0.26, green: 1.0, blue: 0.73, alpha: 1)
        let coral = NSColor(srgbRed: 1.0, green: 0.42, blue: 0.30, alpha: 1)
        let main = level >= 3 ? coral : (done && level == 0 ? mint : gold)
        let edge = NSColor(srgbRed: 0.20, green: 0.14, blue: 0.0, alpha: 0.55)
        let badge: NSColor = level >= 3 ? NSColor(srgbRed: 1.0, green: 0.23, blue: 0.19, alpha: 1)
            : level == 2 ? NSColor(srgbRed: 0.96, green: 0.45, blue: 0.14, alpha: 1)
            : NSColor(srgbRed: 0.96, green: 0.65, blue: 0.14, alpha: 1)
        let width: CGFloat = level > 0 ? 25 : 20
        let image = NSImage(size: NSSize(width: width, height: 20), flipped: false) { rect in
            let c = NSPoint(x: 10, y: rect.midY)
            NSGradient(colors: [main.withAlphaComponent(0.55), main.withAlphaComponent(0)])?
                .draw(fromCenter: c, radius: 0, toCenter: c, radius: 9.6, options: [])
            // the pulse ring of the in-game marker, caught mid-ripple
            let halo = NSBezierPath(ovalIn: NSRect(x: c.x - 8.4, y: c.y - 8.4, width: 16.8, height: 16.8))
            halo.lineWidth = 0.9
            main.withAlphaComponent(0.45).setStroke()
            halo.stroke()
            let ring = NSBezierPath(ovalIn: NSRect(x: c.x - 5.6, y: c.y - 5.6, width: 11.2, height: 11.2))
            ring.lineWidth = 3.0
            edge.setStroke()
            ring.stroke()
            ring.lineWidth = 1.9
            main.setStroke()
            ring.stroke()
            edge.setFill()
            NSBezierPath(ovalIn: NSRect(x: c.x - 3.2, y: c.y - 3.2, width: 6.4, height: 6.4)).fill()
            main.setFill()
            NSBezierPath(ovalIn: NSRect(x: c.x - 2.7, y: c.y - 2.7, width: 5.4, height: 5.4)).fill()
            if level > 0 {   // the "!" giver badge, top right
                let b = NSPoint(x: 19.6, y: 14.2)
                let alpha: CGFloat = (level >= 3 && !pulse) ? 0.35 : 1
                NSColor.black.withAlphaComponent(0.45 * alpha).setFill()
                NSBezierPath(ovalIn: NSRect(x: b.x - 5.4, y: b.y - 5.4, width: 10.8, height: 10.8)).fill()
                badge.withAlphaComponent(alpha).setFill()
                NSBezierPath(ovalIn: NSRect(x: b.x - 4.8, y: b.y - 4.8, width: 9.6, height: 9.6)).fill()
                NSColor.white.withAlphaComponent(alpha).setFill()
                NSBezierPath(roundedRect: NSRect(x: b.x - 0.95, y: b.y - 0.5, width: 1.9, height: 4.1), xRadius: 0.95, yRadius: 0.95).fill()
                NSBezierPath(ovalIn: NSRect(x: b.x - 1.05, y: b.y - 3.55, width: 2.1, height: 2.1)).fill()
            }
            return true
        }
        image.isTemplate = false
        return image
    }

    func buildMenu() {
        menu.removeAllItems()
        for u in urgent.prefix(5) where u.level() > 0 {
            let m = NSMenuItem(title: "!  \(u.title) · \(u.countdown())", action: #selector(openUrgent(_:)), keyEquivalent: "")
            m.target = self
            m.representedObject = u.url
            m.isEnabled = u.url != nil
            menu.addItem(m)
        }
        if urgent.contains(where: { $0.level() > 0 }) { menu.addItem(.separator()) }
        if let t = tracking {
            let m = NSMenuItem(title: "▶  Tracking: \(shorten(t.title)) · \(t.elapsed)", action: nil, keyEquivalent: "")
            m.submenu = questMenu(id: t.id, title: t.title, tracked: true, date: nil)
            menu.addItem(m)
        }
        let others = todayItems.filter { $0.id != tracking?.id }
        if !others.isEmpty || tracking != nil { menu.addItem(.separator()) }
        if !others.isEmpty {
            let head = NSMenuItem(title: "Today's quests", action: nil, keyEquivalent: "")
            head.isEnabled = false
            menu.addItem(head)
            for q in others.prefix(8) {
                let icon = q.type == "main" ? "⭐" : q.type == "fun" ? "🎮" : "🧩"
                let m = NSMenuItem(title: "\(icon)  \(shorten(q.title))", action: nil, keyEquivalent: "")
                m.submenu = questMenu(id: q.id, title: q.title, tracked: false, date: q.date)
                menu.addItem(m)
            }
            menu.addItem(.separator())
        }
        if !notionOK {
            let m = NSMenuItem(title: "⚠  Notion isn't connected: set it up…", action: #selector(openSetup), keyEquivalent: "")
            m.target = self
            menu.addItem(m)
            menu.addItem(.separator())
        }
        let open = NSMenuItem(title: "Open Quest Log", action: #selector(openDefault), keyEquivalent: "q")
        open.keyEquivalentModifierMask = [.control, .option]
        open.target = self
        menu.addItem(open)
        let add = NSMenuItem(title: "Add deadline…", action: #selector(addDeadline), keyEquivalent: "d")
        add.keyEquivalentModifierMask = [.control, .option]
        add.target = self
        menu.addItem(add)
        menu.addItem(.separator())
        for (title, kind) in [("Morning briefing", "morning"), ("Midday check", "midday"), ("Evening report", "evening"), ("Weekly reset (last one)", "weekly")] {
            let m = NSMenuItem(title: title, action: #selector(openKind(_:)), keyEquivalent: "")
            m.target = self
            m.representedObject = kind
            menu.addItem(m)
        }
        menu.addItem(.separator())
        let board = NSMenuItem(title: "Open the board in Notion", action: #selector(openBoard), keyEquivalent: "")
        board.target = self
        menu.addItem(board)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit the Quest Log button", action: #selector(quit), keyEquivalent: "")
        quit.target = self
        menu.addItem(quit)
    }

    /// Today's progress next to the icon; nothing when the saved status is from another day.
    func refresh() {
        var text = ""
        var tip = "Quest Log: click to open · ⌃⌥Q"
        if let data = try? Data(contentsOf: statusFile),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let day = obj["date"] as? String, let done = obj["done"] as? Int, let total = obj["total"] as? Int,
           day == todayString(), total > 0 {
            text = done >= total ? " ✓" : " \(done)/\(total)"
            // The numbers are from the last morning or evening card, not live: say when.
            let asOf = (obj["updatedAt"] as? String).flatMap { hourMinute($0) }.map { " as of \($0)" } ?? ""
            tip = "Quest Log: \(done) of \(total) quests cleared\(asOf) · click to open"
        }
        tracking = loadTracking()
        todayItems = loadToday(todayKey: todayString())
        if let t = tracking {
            text = " ▶ " + t.elapsed
            tip = "Tracking: \(t.title) · \(t.elapsed) · click to open the card"
        }
        if !running.isEmpty { text = " …" }   // a click is being served (building the card can take a few seconds)
        checkToken()
        if !notionOK {
            text += " ⚠"
            tip = "Quest Log: Notion isn't connected (no token in the Keychain), so live cards, deadlines and quick add are off · right-click to set it up"
        }
        if let f = flash, f.until > Date() {
            text = " " + f.text
            if !tooltipNote.isEmpty { tip = tooltipNote }
        } else { flash = nil }
        loadUrgent()
        let worst = urgent.first?.level() ?? 0
        if let first = urgent.first, worst > 0 {
            let more = urgent.filter { $0.level() > 0 }.count - 1
            tip = "! \(first.title): \(first.countdown())" + (more > 0 ? " (+\(more) more)" : "") + " · click to open"
        }
        let key = "\(text == " ✓")-\(worst)-\(worst >= 3 ? pulse : true)-\(notionOK)-\(tracking?.id ?? "")-\(todayItems.map { $0.id }.joined())"
        if key != lastIconKey {   // redraw only when something changed
            lastIconKey = key
            item.button?.image = icon(done: text == " ✓", level: worst, pulse: pulse)
            buildMenu()
        }
        item.button?.attributedTitle = NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)])
        item.button?.toolTip = tip
    }

    /// runs/urgent.json: the quests that have a deadline, soonest first. Levels are worked out here from the clock, so the
    /// badge turns orange and red as time passes even with no network.
    func loadUrgent() {
        var items: [Urgent] = []
        if let data = try? Data(contentsOf: urgentFile),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let list = obj["items"] as? [[String: Any]] {
            for e in list {
                guard let title = e["title"] as? String, let raw = e["due"] as? String, let due = parseDue(raw),
                      due.timeIntervalSinceNow > -3 * 86400 else { continue }
                items.append(Urgent(title: title, due: due, url: e["url"] as? String))
            }
        }
        urgent = items.sorted { $0.due < $1.due }
    }

    func shorten(_ text: String) -> String { text.count > 44 ? String(text.prefix(43)) + "…" : text }

    /// The actions of one quest: Clear, Track / Stop tracking, Move to tomorrow, Open in Notion.
    func questMenu(id: String, title: String, tracked: Bool, date: String?) -> NSMenu {
        let sub = NSMenu()
        func add(_ label: String, _ action: QuestAction) {
            let m = NSMenuItem(title: label, action: #selector(applyQuest(_:)), keyEquivalent: "")
            m.target = self
            m.representedObject = action
            sub.addItem(m)
        }
        add("✓  Clear", QuestAction(id: id, title: title, status: "✅ Cleared", flash: "✓ cleared"))
        if tracked {
            add("■  Stop tracking", QuestAction(id: id, title: title, status: "📜 To Do", flash: "■ stopped"))
        } else {
            add("▶  Track (start now)", QuestAction(id: id, title: title, status: "🔥 Grinding", flash: "▶ tracking"))
        }
        let tomorrow = dayAfter(max(todayString(), date ?? ""))
        add("→  Move to tomorrow", QuestAction(id: id, title: title, status: "📜 To Do", date: tomorrow, flash: "→ moved"))
        sub.addItem(.separator())
        let open = NSMenuItem(title: "Open in Notion", action: #selector(openUrgent(_:)), keyEquivalent: "")
        open.target = self
        open.representedObject = "https://app.notion.com/p/" + id
        sub.addItem(open)
        return sub
    }

    /// "2026-10-20" -> "2026-10-21", Gregorian and POSIX whatever the region settings.
    func dayAfter(_ day: String) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: day), let next = Calendar(identifier: .gregorian).date(byAdding: .day, value: 1, to: d) else { return day }
        return f.string(from: next)
    }

    /// A status action from the menu: queue it like a card click, then write the queue to Notion if a token is saved.
    @objc func applyQuest(_ sender: NSMenuItem) {
        guard let a = sender.representedObject as? QuestAction else { return }
        var changes = [a]
        if a.status == "🔥 Grinding", let t = tracking, t.id != a.id {   // one tracked quest at a time, like the card
            changes.insert(QuestAction(id: t.id, title: t.title, status: "📜 To Do", flash: ""), at: 0)
        }
        var actions: [[String: Any]] = []
        if let data = try? Data(contentsOf: queueFile),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            actions = (obj["actions"] as? [[String: Any]]) ?? []
        }
        let stamper = ISO8601DateFormatter()
        stamper.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        for c in changes {
            actions.removeAll { ($0["id"] as? String) == c.id }   // a newer click supersedes an older queued one
            var e: [String: Any] = ["id": c.id, "status": c.status, "title": c.title, "at": stamper.string(from: Date())]
            if let d = c.date { e["date"] = d }
            actions.append(e)
        }
        guard let out = try? JSONSerialization.data(withJSONObject: ["version": 1, "actions": actions], options: [.prettyPrinted]) else {
            NSSound.beep()
            return
        }
        do {
            try out.write(to: queueFile, options: .atomic)
        } catch {
            NSSound.beep()
            return
        }
        flash = (a.flash, Date().addingTimeInterval(3))
        tooltipNote = ""
        lastIconKey = ""
        refresh()
        if notionOK { runPython(["apply"]) }
    }

    /// `report.py <args>` in the background; the icon refreshes when it ends.
    func runPython(_ args: [String]) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [reportPy] + args
        p.environment = ["PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = logHandle()
        p.terminationHandler = { _ in DispatchQueue.main.async { [weak self] in self?.refresh() } }
        try? p.run()
    }

    /// Is a Notion token saved (and not empty: an empty paste leaves an empty Keychain item)? Asks `security`, at most once a
    /// minute; the secret is only measured here, never kept, logged or sent anywhere.
    func checkToken() {
        guard Date().timeIntervalSince(lastTokenCheck) >= 60 else { return }
        lastTokenCheck = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        p.arguments = ["find-generic-password", "-s", "QuestLog Notion", "-a", "token", "-w"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            let length = out.fileHandleForReading.readDataToEndOfFile().count
            p.waitUntilExit()
            notionOK = p.terminationStatus == 0 && length > 20
        } catch {
            notionOK = true   // can't tell: don't nag
        }
    }

    /// Opens set-notion-token.command in Terminal: the token is typed there, so it never passes through this app.
    @objc func openSetup() {
        NSWorkspace.shared.open(setupScript)
        lastTokenCheck = Date.distantPast   // look again soon: it may be saved in a minute
    }

    /// Centre a dialog on the screen the mouse is on (an alert of this menu-bar app would otherwise open on whichever display it picks).
    func centre(_ alert: NSAlert) {
        alert.layout()
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main ?? NSScreen.screens[0]
        let size = alert.window.frame.size
        let area = screen.visibleFrame
        alert.window.setFrameOrigin(NSPoint(x: area.midX - size.width / 2, y: area.midY - size.height / 2 + area.height * 0.12))
    }

    /// ⌃⌥D / menu: one line, "title, when", becomes a quest with a Due in Notion (`report.py add`).
    @objc func addDeadline() {
        if addOpen { return }
        addOpen = true
        defer { addOpen = false }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.icon = icon(done: false, level: 0, pulse: true)   // the quest marker instead of a generic folder
        alert.messageText = "Add a deadline"
        alert.informativeText = "Title, then when it is due:\n  Submit visa form, tomorrow 9am\n  Pay rent in 3h\n  Report, fri 17:00\n  Call the bank, 26/10 10:30"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        field.placeholderString = "Submit visa form, tomorrow 9am"
        field.stringValue = retryText
        retryText = ""
        alert.accessoryView = field
        alert.addButton(withTitle: "Add")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        alert.window.level = .floating
        centre(alert)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { runAdd(text) }
    }

    func runAdd(_ text: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [reportPy, "add", "--", text]
        p.environment = ["PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.terminationHandler = { [weak self] done in
            let said = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            let complained = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.running.removeAll { $0 === done }
                if done.terminationStatus == 0 {
                    self.flash = ("✓ added", Date().addingTimeInterval(4))
                    self.tooltipNote = said.trimmingCharacters(in: .whitespacesAndNewlines)
                    NSSound(named: NSSound.Name("Glass"))?.play()
                } else {
                    self.failedAdd(text: text, code: done.terminationStatus, message: complained.trimmingCharacters(in: .whitespacesAndNewlines))
                }
                self.lastIconKey = ""   // redraw the icon and the menu with the new deadline
                self.refresh()
            }
        }
        do {
            try p.run()
            running.append(p)
            refresh()
        } catch {
            NSSound.beep()
        }
    }

    /// exit 2 = the line wasn't understood (ask again, with the text kept), 3 = no token (offer the setup), else Notion's message.
    func failedAdd(text: String, code: Int32, message: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.icon = icon(done: false, level: 0, pulse: true)
        alert.messageText = code == 2 ? "Couldn't read that" : code == 3 ? "Notion isn't connected" : "Couldn't add the deadline"
        alert.informativeText = message.isEmpty ? "Something went wrong. See runs/bar.log." : message
        alert.window.level = .floating
        centre(alert)
        if code == 3 {
            alert.addButton(withTitle: "Set up…")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() == .alertFirstButtonReturn { openSetup() }
        } else if code == 2 {
            alert.addButton(withTitle: "Try again")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() == .alertFirstButtonReturn {
                retryText = text
                DispatchQueue.main.async { self.addDeadline() }
            }
        } else {
            alert.addButton(withTitle: "OK")
            alert.runModal()
        }
    }

    @objc func openUrgent(_ sender: NSMenuItem) {
        if let raw = sender.representedObject as? String, let url = URL(string: raw) { NSWorkspace.shared.open(url) }
    }

    /// ISO date in the Gregorian calendar and POSIX digits, whatever the user's region settings (status.json uses it).
    func todayString() -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    /// "HH:mm" (local) of an ISO-8601 timestamp such as 2026-10-20T08:16:08+07:00.
    func hourMinute(_ iso: String) -> String? {
        let parser = ISO8601DateFormatter()
        guard let date = parser.date(from: iso) else { return nil }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }

    @objc func clicked(_ sender: NSStatusBarButton) {
        let type = NSApp.currentEvent?.type
        let flags = NSApp.currentEvent?.modifierFlags ?? []
        if type == .rightMouseUp || flags.contains(.control) {
            tracking = loadTracking()
            todayItems = loadToday(todayKey: todayString())
            buildMenu()   // fresh countdowns
            item.menu = menu
            item.button?.performClick(nil)   // shows the menu under the icon
            item.menu = nil
        } else {
            openNow(nil)
        }
    }

    @objc func openDefault() { openNow(nil) }

    @objc func openKind(_ sender: NSMenuItem) { openNow(sender.representedObject as? String) }

    @objc func openBoard() {
        if let url = URL(string: boardURL) { NSWorkspace.shared.open(url) }
    }

    @objc func quit() { NSApp.terminate(nil) }

    /// `report.py now [kind]`: it builds the card (or finds the saved one) and shows it. Taps closer than 1 s are one tap.
    func openNow(_ kind: String?) {
        if Date().timeIntervalSince(lastOpen) < 1 { return }
        lastOpen = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [reportPy, "now"] + (kind.map { [$0] } ?? [])
        p.environment = ["PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = logHandle()
        p.terminationHandler = { [weak self] done in
            DispatchQueue.main.async {
                self?.running.removeAll { $0 === done }
                if done.terminationStatus != 0 || done.terminationReason == .uncaughtSignal { NSSound.beep() }   // nothing was shown
                self?.refresh()
            }
        }
        do {
            try p.run()
            running.append(p)
            refresh()
        } catch {
            NSSound.beep()
        }
    }

    /// A deadline is within 3 hours (or passed): once a minute ask `report.py urgent` whether a step of the escalation ladder
    /// is due (it checks that you are at the Mac and shows the urgent card). Nothing runs while no deadline is near.
    func urgentTick() {
        guard Date().timeIntervalSince(lastUrgentTick) >= 55,
              urgent.contains(where: { $0.due.timeIntervalSinceNow <= 3 * 3600 }) else { return }
        lastUrgentTick = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [reportPy, "urgent"]
        p.environment = ["PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = logHandle()
        p.terminationHandler = { _ in DispatchQueue.main.async { [weak self] in self?.refresh() } }
        try? p.run()
    }

    /// `report.py catchup` a few seconds after a wake-up or an unlock: it shows the card that is due and unseen, and only
    /// if you are at the Mac (idempotent, so a duplicate trigger is harmless). Never beeps: most runs correctly do nothing.
    func catchUp() {
        if Date().timeIntervalSince(lastCatchUp) < 20 { return }
        lastCatchUp = Date()
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = [reportPy, "catchup"]
            p.environment = ["PYTHONIOENCODING": "utf-8", "LANG": "en_US.UTF-8", "HOME": NSHomeDirectory(), "PATH": "/usr/bin:/bin"]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = self?.logHandle() ?? FileHandle.nullDevice
            p.terminationHandler = { _ in DispatchQueue.main.async { self?.refresh() } }
            try? p.run()
        }
    }

    /// runs/bar.log, appended to: why a click showed nothing.
    func logHandle() -> FileHandle {
        let fm = FileManager.default
        try? fm.createDirectory(at: logFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logFile.path) { fm.createFile(atPath: logFile.path, contents: nil) }
        let h = try? FileHandle(forWritingTo: logFile)
        h?.seekToEndOfFile()
        return h ?? FileHandle.nullDevice
    }

    /// ⌃⌥Q (open the card) and ⌃⌥D (add a deadline) anywhere. RegisterEventHotKey needs no Accessibility permission.
    func registerHotKey() {
        hotKeyActions[1] = { [weak self] in self?.openNow(nil) }
        hotKeyActions[2] = { [weak self] in self?.addDeadline() }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            let status = GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                                           nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            if status == noErr { hotKeyActions[id.id]?() }
            return noErr
        }, 1, &spec, nil, nil)
        let sig = OSType(0x514C_4F47)   // 'QLOG'
        RegisterEventHotKey(UInt32(kVK_ANSI_Q), UInt32(controlKey | optionKey), EventHotKeyID(signature: sig, id: 1),
                            GetApplicationEventTarget(), 0, &hotKey)
        RegisterEventHotKey(UInt32(kVK_ANSI_D), UInt32(controlKey | optionKey), EventHotKeyID(signature: sig, id: 2),
                            GetApplicationEventTarget(), 0, &addHotKey)
    }
}

let app = NSApplication.shared
let bar = Bar()
app.delegate = bar
app.run()
