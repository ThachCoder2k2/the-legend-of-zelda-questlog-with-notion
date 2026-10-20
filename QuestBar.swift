// QuestBar: the Quest Log button in the menu bar, the "widget" that opens the card any time.
//
//   click          open the Quest Log card for the time of day (live from Notion, or the last saved card offline)
//   right-click    menu: Morning / Midday / Evening / Weekly, the Notion board, Quit
//   ⌃⌥Q            the same as a click, from anywhere
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
let urgentFile = folder.appendingPathComponent("runs/urgent.json")
let logFile = folder.appendingPathComponent("runs/bar.log")

var onHotKey: (() -> Void)?

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

final class Bar: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var running: [Process] = []
    var lastCatchUp = Date.distantPast
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
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.refresh() }
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
        let open = NSMenuItem(title: "Open Quest Log", action: #selector(openDefault), keyEquivalent: "q")
        open.keyEquivalentModifierMask = [.control, .option]
        open.target = self
        menu.addItem(open)
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
        if !running.isEmpty { text = " …" }   // a click is being served (building the card can take a few seconds)
        loadUrgent()
        let worst = urgent.first?.level() ?? 0
        if let first = urgent.first, worst > 0 {
            let more = urgent.filter { $0.level() > 0 }.count - 1
            tip = "! \(first.title): \(first.countdown())" + (more > 0 ? " (+\(more) more)" : "") + " · click to open"
        }
        let key = "\(text == " ✓")-\(worst)-\(worst >= 3 ? pulse : true)"
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
                guard let title = e["title"] as? String, let raw = e["due"] as? String, let due = parseDue(raw) else { continue }
                items.append(Urgent(title: title, due: due, url: e["url"] as? String))
            }
        }
        urgent = items.sorted { $0.due < $1.due }
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

    /// ⌃⌥Q anywhere. RegisterEventHotKey needs no Accessibility permission.
    func registerHotKey() {
        onHotKey = { [weak self] in self?.openNow(nil) }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            onHotKey?()
            return noErr
        }, 1, &spec, nil, nil)
        let id = EventHotKeyID(signature: OSType(0x514C_4F47), id: 1)   // 'QLOG'
        RegisterEventHotKey(UInt32(kVK_ANSI_Q), UInt32(controlKey | optionKey), id, GetApplicationEventTarget(), 0, &hotKey)
    }
}

let app = NSApplication.shared
let bar = Bar()
app.delegate = bar
app.run()
