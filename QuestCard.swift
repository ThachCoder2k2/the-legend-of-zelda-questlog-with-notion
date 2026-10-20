// QuestCard: shows a Quest Log report card (card.html) in a floating panel.
// Usage: questcard /path/to/data.json
// JSON keys used here: url (the board, opened by "Board"), timeoutSec, sound ("" = silent). The rest is rendered by card.html.
// Sounds: sounds/*.wav next to this binary (made by make_sounds.py); card.html asks for them by name.
//
// The card never takes the keyboard on its own: it pops up without focus, and keys reach it only after
// the user clicks it, so typing in another app is never captured.
//
// Marking a quest done from the card: every click is first written to pending.json (next to this binary)
// so it cannot be lost. With a Notion integration token in the login Keychain (service "QuestLog Notion",
// account "token") the Status is also written to Notion after a short grace period (an undo inside it
// sends nothing), and the queue entry is dropped once Notion confirms. Without a token, the next scheduled
// Quest Log task applies the queue (see pending.py).

import Cocoa
import WebKit

let boardURL = "notion://www.notion.so/3fe207ad5a6f81858d5fcde429bff7cf"
let keychainService = "QuestLog Notion"
let allowedStatuses: Set<String> = ["✅ Cleared", "📜 To Do", "🔥 Grinding", "⏸ Delay"]
let cardOpenedNotification = Notification.Name("local.questlog.card.opened")
let holdSeconds: TimeInterval = 1.5          // grace period before a click is sent to Notion
let claimWindow: TimeInterval = 15 * 60      // a queue entry listed by a scheduled task this recently may be in flight

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// Takes the first click even when the panel is not key, and lets the card header drag the window.
final class CardWebView: WKWebView {
    var dragZone: CGFloat = 0
    var dragX: ClosedRange<CGFloat> = 0...CGFloat.greatestFiniteMagnitude   // only the card's own header drags, not the side panel
    var onDragEnd: (() -> Void)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        let fromTop = isFlipped ? p.y : bounds.height - p.y
        if dragZone > 0, fromTop >= 0, fromTop < dragZone, dragX.contains(p.x), let window = window {
            window.performDrag(with: event)   // returns on mouse-up
            onDragEnd?()
            return
        }
        super.mouseDown(with: event)
    }
}

/// Reads the Notion token with /usr/bin/security, which owns the Keychain item, so no access prompt appears.
func readNotionToken() -> String? {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
    p.arguments = ["find-generic-password", "-s", keychainService, "-a", "token", "-w"]
    let out = Pipe()
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    do { try p.run() } catch { return nil }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    guard p.terminationStatus == 0,
          let s = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
          !s.isEmpty else { return nil }
    return s
}

/// Only Notion pages may be opened from the card.
func safeNotionURL(_ raw: String?) -> URL? {
    guard let raw = raw, let url = URL(string: raw), let host = url.host?.lowercased() else { return nil }
    switch url.scheme?.lowercased() {
    case "notion": return host == "www.notion.so" ? url : nil
    case "https": return ["www.notion.so", "notion.so", "app.notion.com"].contains(host) ? url : nil
    default: return nil
    }
}

struct StatusChange {
    let id: String
    let status: String
    let date: String?            // "YYYY-MM-DD" when the quest is moved to another day
    let title: String
    var at: String = ""          // stamp of its pending.json entry
}

final class CardController: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    let json: String
    let htmlURL: URL
    let queueURL: URL
    let openURL: URL?
    let timeout: TimeInterval
    let deadline: Date
    let soundName: String?
    let token: String?
    var width: CGFloat = 440          // 440 = card; wider while the detail panel is open beside it
    var mode: String                  // "full" = covers the screen like the game menu; "compact" = corner card
    var compactFrame: NSRect?         // where the corner card was before going full screen
    var lastHeight: CGFloat = 420     // the compact card's height, as last reported by the page
    var pendingFrame: NSRect?         // target of the frame animation in flight; the next change builds on it
    var animToken = 0

    var panel: KeyPanel!
    var web: CardWebView!
    var shown = false
    var closing = false
    var terminating = false

    // Token path: held = clicks inside the grace period; outbox = released, sent one at a time so a
    // quick done → undo lands in order; onNotion = quests with a write that reached (or may have reached) Notion.
    var held: [String: (change: StatusChange, work: DispatchWorkItem)] = [:]
    var outbox: [StatusChange] = []
    var sending: StatusChange?
    var onNotion: Set<String> = []

    // Menu sounds, preloaded; the short ones get a few copies so quick cursor moves can overlap.
    var sfx: [String: [NSSound]] = [:]
    var soundsOn: Bool { soundName != nil && soundName != "" }

    /// sounds/custom/<name>.<ext> (the user's own files) wins over the built-in sounds/<name>.wav.
    func loadSounds() {
        let dir = htmlURL.deletingLastPathComponent().appendingPathComponent("sounds")
        let custom = dir.appendingPathComponent("custom")
        let fm = FileManager.default
        for (name, copies) in [("open", 1), ("move", 4), ("scroll", 4), ("select", 2), ("done", 2), ("undo", 2), ("cancel", 2), ("track", 2), ("untrack", 2), ("close", 1), ("alert", 1)] {
            let mine = ["wav", "aiff", "aif", "caf", "m4a", "mp3"].map { custom.appendingPathComponent(name + "." + $0) }
                .first { fm.fileExists(atPath: $0.path) }
            let url = mine ?? dir.appendingPathComponent(name + ".wav")
            let pool = (0..<copies).compactMap { _ in NSSound(contentsOf: url, byReference: true) }
            if !pool.isEmpty { sfx[name] = pool }
        }
    }

    func play(_ name: String) {
        guard soundsOn else { return }
        if let pool = sfx[name] {
            let sound = pool.first(where: { !$0.isPlaying }) ?? pool[0]
            if sound.isPlaying { sound.stop() }
            sound.play()
        } else {
            NSSound(named: NSSound.Name(name))?.play()   // a system sound name from the card JSON
        }
    }

    var soundPlaying: Bool { sfx.values.contains { $0.contains { $0.isPlaying } } }

    init(json: String, config: [String: Any], htmlURL: URL) {
        self.json = json
        self.htmlURL = htmlURL
        self.queueURL = htmlURL.deletingLastPathComponent().appendingPathComponent("pending.json")
        self.openURL = safeNotionURL(config["url"] as? String) ?? URL(string: boardURL)
        self.timeout = (config["timeoutSec"] as? Double) ?? 1800
        self.deadline = Date().addingTimeInterval(timeout)
        self.soundName = (config["sound"] as? String) ?? "open"
        self.token = readNotionToken()
        self.mode = (config["display"] as? String) == "compact" ? "compact" : "full"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        loadSounds()
        let controller = WKUserContentController()
        controller.add(self, name: "ql")
        let maxHeight = Int(targetScreen().visibleFrame.height - 40)
        let env = "{\"notion\": \(token != nil), \"queue\": true, \"maxHeight\": \(maxHeight), \"mode\": \"\(mode)\"}"
        controller.addUserScript(WKUserScript(source: "window.QL_DATA = \(json); window.QL_ENV = \(env);",
                                              injectionTime: .atDocumentStart, forMainFrameOnly: true))
        let config = WKWebViewConfiguration()
        config.userContentController = controller

        let frame = NSRect(x: 0, y: 0, width: width, height: 320)
        panel = KeyPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovable = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.alphaValue = 0

        web = CardWebView(frame: frame, configuration: config)
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.onDragEnd = { [weak self] in self?.pendingFrame = nil }
        panel.contentView = web
        panel.initialFirstResponder = web

        web.loadFileURL(htmlURL, allowingReadAccessTo: htmlURL.deletingLastPathComponent())

        // Safety net: show the card even if the page never reports its height.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            self?.show(height: 420)
        }
        // Auto-close on the wall clock, so a card does not survive a night of sleep.
        DispatchQueue.main.asyncAfter(wallDeadline: .now() + timeout) { [weak self] in
            self?.dismiss()
        }
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self = self, Date() >= self.deadline else { return }
            self.dismiss()
        }
        // A newer card replaces this one (its unsent clicks are flushed by dismiss()).
        DistributedNotificationCenter.default().addObserver(self, selector: #selector(otherCardOpened(_:)), name: cardOpenedNotification,
                                                            object: nil, suspensionBehavior: .deliverImmediately)
        DistributedNotificationCenter.default().postNotificationName(cardOpenedNotification, object: myPID, userInfo: nil, deliverImmediately: true)
    }

    let myPID = String(ProcessInfo.processInfo.processIdentifier)

    @objc func otherCardOpened(_ note: Notification) {
        guard let sender = note.object as? String, sender != myPID else { return }
        DispatchQueue.main.async { self.dismiss() }
    }

    func targetScreen() -> NSScreen {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.main ?? NSScreen.screens[0]
    }

    func show(height: CGFloat) {
        guard !shown, !closing else { return }
        shown = true
        lastHeight = height
        if mode == "full" {   // the whole screen, above the menu bar, like the game's menu
            let screen = targetScreen()
            panel.level = .statusBar
            panel.setFrame(screen.frame, display: true)
            panel.orderFrontRegardless()
            if let name = soundName, !name.isEmpty { play(name) }
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.3
                panel.animator().alphaValue = 1
            }
            web.evaluateJavaScript("window.QL_GO && window.QL_GO()", completionHandler: nil)
            return
        }
        let visible = targetScreen().visibleFrame
        let h = min(max(height, 120), visible.height - 40)
        let x = visible.maxX - width - 18
        let y = visible.maxY - h - 14
        panel.setFrame(NSRect(x: x + 36, y: y, width: width, height: h), display: true)
        // No makeKey(): the card must not take the keyboard from the app the user is typing in.
        // The first click on it makes the panel key (nonactivating, so the other app stays active).
        panel.orderFrontRegardless()
        if let name = soundName, !name.isEmpty { play(name) }
        let target = NSRect(x: x, y: y, width: width, height: h)
        pendingFrame = target   // a resize during the slide-in builds on where the card is going, not where it is
        animToken += 1
        let token = animToken
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.42
            ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.2, 0.9, 0.25, 1)
            panel.animator().alphaValue = 1
            panel.animator().setFrame(target, display: true)
        }, completionHandler: { [weak self] in
            guard let self = self, self.animToken == token else { return }
            self.pendingFrame = nil
        })
        web.evaluateJavaScript("window.QL_GO && window.QL_GO()", completionHandler: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.panel.invalidateShadow() }
    }

    func animate(to f: NSRect, duration: TimeInterval) {
        pendingFrame = f
        animToken += 1
        let token = animToken
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = duration
            ctx.timingFunction = CAMediaTimingFunction(controlPoints: 0.2, 0.8, 0.2, 1)
            panel.animator().setFrame(f, display: true)
        }, completionHandler: { [weak self] in
            guard let self = self else { return }
            if self.animToken == token { self.pendingFrame = nil }
            self.panel.invalidateShadow()
        })
    }

    // Keep the top edge fixed and grow/shrink downward.
    func resize(height: CGFloat) {
        lastHeight = height
        guard shown, !closing, mode == "compact", let screen = panel.screen ?? NSScreen.main else { return }
        let h = min(max(height, 120), screen.visibleFrame.height - 40)
        var f = pendingFrame ?? panel.frame
        if abs(f.height - h) < 1 { return }
        let top = f.maxY
        f.size.height = h
        f.origin.y = top - h
        animate(to: f, duration: 0.18)
    }

    // The detail panel opens beside the card: grow toward the side with room (left when the card sits at the
    // right edge, as it does by default), keeping the card itself still on screen where possible.
    func expand(to newWidth: CGFloat) {
        guard shown, !closing, mode == "compact", let screen = panel.screen ?? NSScreen.main, abs(newWidth - width) >= 1 else { return }
        let visible = screen.visibleFrame
        let base = pendingFrame ?? panel.frame
        var f = base
        let grow = newWidth - width
        if grow > 0 {
            f.origin.x = (f.minX - grow >= visible.minX + 8) ? f.minX - grow : f.minX   // room on the left?
            f.origin.x = min(f.origin.x, visible.maxX - newWidth - 8)
        } else if anchoredRight {
            f.origin.x = f.minX - grow
        }
        anchoredRight = grow > 0 && f.origin.x < base.minX
        if grow > 0 {   // tell the page where the panel goes: left of the card when the window grows leftward
            web.evaluateJavaScript("window.QL_SIDE && window.QL_SIDE('\(anchoredRight ? "left" : "right")')", completionHandler: nil)
        }
        f.size.width = newWidth
        width = newWidth
        animate(to: f, duration: 0.22)
    }
    var anchoredRight = false

    // Full screen <-> corner card (M key, or a click on the dimmed backdrop).
    func setLayout(_ newMode: String) {
        guard shown, !closing, newMode != mode, let screen = panel.screen ?? NSScreen.main else { return }
        mode = newMode
        let target: NSRect
        if newMode == "full" {
            var c = pendingFrame ?? panel.frame   // remember the card's own spot, not the detail panel's beside it
            if anchoredRight { c.origin.x = c.maxX - 440 }
            c.size.width = 440
            compactFrame = c
            panel.level = .statusBar
            target = screen.frame
        } else {
            panel.level = .floating
            let visible = screen.visibleFrame
            let h = min(max(lastHeight, 120), visible.height - 40)
            if let c = compactFrame {
                target = NSRect(x: c.minX, y: c.maxY - h, width: 440, height: h)
            } else {
                target = NSRect(x: visible.maxX - 440 - 18, y: visible.maxY - h - 14, width: 440, height: h)
            }
            width = 440
            anchoredRight = false
        }
        animate(to: target, duration: 0.24)
    }

    func dismiss(open url: URL? = nil) {
        guard !closing else { return }
        closing = true
        flushHeld()
        if let url = url { NSWorkspace.shared.open(url) }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.22
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            self?.panel.orderOut(nil)
            self?.terminateWhenIdle()
        })
        // Never hang on the network: every click is already in pending.json, so after 6 s just quit.
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { [weak self] in
            guard let self = self, !self.terminating else { return }
            self.terminating = true
            NSApp.terminate(nil)
        }
    }

    func terminateWhenIdle(waited: Int = 0) {
        guard closing, held.isEmpty, sending == nil, outbox.isEmpty, !terminating else { return }
        // let the close/select sound finish (at most ~1.5 s)
        if soundPlaying && waited < 15 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.terminateWhenIdle(waited: waited + 1) }
            return
        }
        terminating = true
        NSApp.terminate(nil)
    }

    // MARK: status changes

    func reply(_ id: String, _ state: String, _ info: String = "") {
        let args = (try? JSONSerialization.data(withJSONObject: [id, state, info])).flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        web.evaluateJavaScript("window.QL_STATUS && window.QL_STATUS(...\(args))", completionHandler: nil)
    }

    func requestStatus(id rawID: String, status: String, date: String?, title: String, revert: Bool) {
        let id = rawID.lowercased()
        guard id.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil, allowedStatuses.contains(status),
              date == nil || date!.range(of: "^\\d{4}-\\d{2}-\\d{2}$", options: .regularExpression) != nil else {
            reply(rawID, "error", "bad request")
            return
        }
        var change = StatusChange(id: id, status: status, date: date, title: title)

        // A newer click supersedes an older one that has not been sent yet.
        if let h = held.removeValue(forKey: id) { h.work.cancel() }
        outbox.removeAll { $0.id == id }

        // Undo of a click that never reached Notion: drop it instead of writing anything.
        if revert, !onNotion.contains(id), sending?.id != id {
            if dropQueued(id) { reply(id, "local") } else { enqueue(&change); reply(id, "queued") }
            return
        }

        enqueue(&change)   // durable copy first, so the click survives a crash or a failed send
        guard token != nil else { reply(id, "queued"); return }
        let work = DispatchWorkItem { [weak self] in self?.release(id) }
        held[id] = (change, work)
        reply(id, "sending")
        DispatchQueue.main.asyncAfter(deadline: .now() + holdSeconds, execute: work)
    }

    func release(_ id: String) {
        guard let h = held.removeValue(forKey: id) else { return }
        outbox.append(h.change)
        pump()
    }

    func flushHeld() {
        for (id, h) in held { h.work.cancel(); outbox.removeAll { $0.id == id }; outbox.append(h.change) }
        held.removeAll()
        pump()
    }

    func pump() {
        guard sending == nil, !outbox.isEmpty, let token = token else { return }
        let change = outbox.removeFirst()
        sending = change
        onNotion.insert(change.id)
        var req = URLRequest(url: URL(string: "https://api.notion.com/v1/pages/\(change.id)")!)
        req.httpMethod = "PATCH"
        req.timeoutInterval = 12
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("2022-06-28", forHTTPHeaderField: "Notion-Version")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var props: [String: Any] = ["Status": ["status": ["name": change.status]]]
        if let date = change.date { props["Date"] = ["date": ["start": date]] }
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["properties": props])
        URLSession.shared.dataTask(with: req) { [weak self] _, response, error in
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            let urlError = (error as? URLError)?.code
            DispatchQueue.main.async {
                guard let self = self, !self.terminating else { return }
                self.sending = nil
                let newer = self.held[change.id] != nil || self.outbox.contains { $0.id == change.id }
                if code == 200 {
                    self.removeQueued(change.id, at: change.at)
                    self.recordApplied(change)
                    if !newer { self.reply(change.id, "synced") }
                } else {
                    // Notion answered with an error, or the request never left: it was not applied.
                    // A timeout is ambiguous, so the quest stays marked as possibly written.
                    let notSent: Set<URLError.Code> = [.notConnectedToInternet, .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed, .networkConnectionLost]
                    if code != 0 || (urlError.map { notSent.contains($0) } ?? false) { self.onNotion.remove(change.id) }
                    let why = code == 401 ? "bad token" : code == 404 ? "page not shared with the integration" : code != 0 ? "Notion \(code)" : "offline"
                    if !newer { self.reply(change.id, "queued", why) }   // its pending.json copy is still there
                }
                self.pump()
                self.terminateWhenIdle()
            }
        }.resume()
    }

    // MARK: pending.json

    /// Same ledger as pending.py `done` (applied.json, 14 days), so the evening stats see clicks sent straight to Notion.
    func recordApplied(_ change: StatusChange) {
        let url = queueURL.deletingLastPathComponent().appendingPathComponent("applied.json")
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let now = Date()
        let cutoff = f.string(from: now.addingTimeInterval(-14 * 86400))
        var ledger: [[String: Any]] = []
        if let data = try? Data(contentsOf: url), let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            ledger = ((obj["applied"] as? [[String: Any]]) ?? []).filter { ($0["applied_at"] as? String ?? "") >= cutoff }
        }
        var entry: [String: Any] = ["id": change.id, "status": change.status, "at": change.at, "applied_at": f.string(from: now), "via": "card"]
        if let date = change.date { entry["date"] = date }
        ledger.append(entry)
        if let data = try? JSONSerialization.data(withJSONObject: ["version": 1, "applied": ledger], options: [.prettyPrinted]) {
            try? data.write(to: url, options: .atomic)
        }
    }

    func loadQueue() -> [[String: Any]] {
        guard let data = try? Data(contentsOf: queueURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [] }
        return (obj["actions"] as? [[String: Any]]) ?? []
    }

    func saveQueue(_ actions: [[String: Any]]) {
        guard let data = try? JSONSerialization.data(withJSONObject: ["version": 1, "actions": actions], options: [.prettyPrinted]) else { return }
        try? data.write(to: queueURL, options: .atomic)
    }

    func enqueue(_ change: inout StatusChange) {
        var actions = loadQueue().filter { ($0["id"] as? String) != change.id }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        change.at = f.string(from: Date())
        var entry: [String: Any] = ["id": change.id, "status": change.status, "title": change.title, "at": change.at]
        if let date = change.date { entry["date"] = date }
        actions.append(entry)
        saveQueue(actions)
    }

    /// Removes the entry for id (only the one stamped `at`, if given).
    @discardableResult
    func removeQueued(_ id: String, at: String? = nil) -> Bool {
        let actions = loadQueue()
        let kept = actions.filter { ($0["id"] as? String) != id || (at != nil && ($0["at"] as? String) != at) }
        if kept.count == actions.count { return false }
        saveQueue(kept)
        return true
    }

    /// Drops a queued click that is being undone. Refuses (returns false) when there is no entry, or when a
    /// scheduled task listed it recently and may be writing it right now; the caller then queues the undo.
    func dropQueued(_ id: String) -> Bool {
        guard let entry = loadQueue().first(where: { ($0["id"] as? String) == id }) else { return false }
        if let claimed = entry["claimedAt"] as? Double, Date().timeIntervalSince1970 - claimed < claimWindow { return false }
        return removeQueued(id)
    }

    // MARK: messages from card.html

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "ready", "resize":
            let h = (body["height"] as? NSNumber).map { CGFloat(truncating: $0) } ?? 420
            if shown { resize(height: h) } else { show(height: h) }
        case "dragzone":
            web.dragZone = (body["height"] as? NSNumber).map { CGFloat(truncating: $0) } ?? 0
            if let x0 = body["x0"] as? NSNumber, let x1 = body["x1"] as? NSNumber, x1.doubleValue > x0.doubleValue {
                web.dragX = CGFloat(truncating: x0)...CGFloat(truncating: x1)
            }
        case "layout":
            if let m = body["mode"] as? String, m == "full" || m == "compact" { setLayout(m) }
        case "expand":
            if let w = body["width"] as? NSNumber { expand(to: min(max(CGFloat(truncating: w), 440), 960)) }
        case "open":
            dismiss(open: safeNotionURL(body["url"] as? String) ?? openURL)
        case "status":
            guard !closing else { return }
            requestStatus(id: body["id"] as? String ?? "", status: body["status"] as? String ?? "", date: body["date"] as? String,
                          title: body["title"] as? String ?? "", revert: body["revert"] as? Bool ?? false)
        case "sfx":
            if let name = body["name"] as? String, sfx[name] != nil { play(name) }
        case "snooze":   // the urgent card: hold the deadline alerts (urgent.py keeps the snooze), then close
            guard !closing else { return }
            let minutes = min(max((body["minutes"] as? NSNumber)?.intValue ?? 15, 1), 240)
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [htmlURL.deletingLastPathComponent().appendingPathComponent("urgent.py").path, "snooze", String(minutes)]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            if (try? p.run()) != nil { p.waitUntilExit() }
            dismiss()
        case "close":
            dismiss()
        default:
            break
        }
    }

    // Links inside the card open in the default handler instead of the card.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url {
            if let safe = safeNotionURL(url.absoluteString) { NSWorkspace.shared.open(safe) }
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }
}

let args = CommandLine.arguments
guard args.count >= 2,
      let raw = try? Data(contentsOf: URL(fileURLWithPath: args[1])),
      let object = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
      let normalized = try? JSONSerialization.data(withJSONObject: object),
      let json = String(data: normalized, encoding: .utf8) else {
    FileHandle.standardError.write("usage: questcard <data.json> (valid JSON object)\n".data(using: .utf8)!)
    exit(1)
}

let executable = (Bundle.main.executableURL ?? URL(fileURLWithPath: args[0])).resolvingSymlinksInPath()
let html = executable.deletingLastPathComponent().appendingPathComponent("card.html")
guard FileManager.default.fileExists(atPath: html.path) else {
    FileHandle.standardError.write("card.html not found next to questcard\n".data(using: .utf8)!)
    exit(2)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = CardController(json: json, config: object, htmlURL: html)
app.delegate = controller
app.run()
