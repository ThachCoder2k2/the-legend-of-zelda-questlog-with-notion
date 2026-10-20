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

var onHotKey: (() -> Void)?

final class Bar: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var running: [Process] = []
    var lastOpen = Date.distantPast

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = item.button {
            b.image = icon()
            b.imagePosition = .imageLeft
            b.target = self
            b.action = #selector(clicked(_:))
            b.sendAction(on: [.leftMouseUp, .rightMouseUp])
            b.toolTip = "Quest Log: click to open · ⌃⌥Q"
        }
        buildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.refresh() }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.refresh()
        }
        registerHotKey()
    }

    /// The tracking target of the card, drawn as a template image so it follows the menu bar's light or dark look.
    func icon() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { rect in
            NSColor.black.set()
            let ring = NSBezierPath(ovalIn: rect.insetBy(dx: 3, dy: 3))
            ring.lineWidth = 1.6
            ring.stroke()
            NSBezierPath(ovalIn: NSRect(x: 6.6, y: 6.6, width: 4.8, height: 4.8)).fill()
            return true
        }
        image.isTemplate = true
        return image
    }

    func buildMenu() {
        menu.removeAllItems()
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
            tip = "Quest Log: \(done) of \(total) quests cleared today · click to open"
        }
        item.button?.attributedTitle = NSAttributedString(string: text, attributes: [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)])
        item.button?.toolTip = tip
    }

    func todayString() -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: Date())
    }

    @objc func clicked(_ sender: NSStatusBarButton) {
        let type = NSApp.currentEvent?.type
        let flags = NSApp.currentEvent?.modifierFlags ?? []
        if type == .rightMouseUp || flags.contains(.control) {
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
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] done in
            DispatchQueue.main.async {
                self?.running.removeAll { $0 === done }
                self?.refresh()
            }
        }
        do {
            try p.run()
            running.append(p)
        } catch {
            NSSound.beep()
        }
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
