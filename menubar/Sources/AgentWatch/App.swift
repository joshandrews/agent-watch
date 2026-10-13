import AppKit
import Combine
import SwiftUI

// Agent Watch: the menu bar face of claude-watcher.
//   AgentWatch                                   run in the menu bar
//   AgentWatch --snapshot out.png [--root dir] [--dark] [--height 900]
//                                                render the window to a PNG (used by the tests)
//   AGENTWATCH_SELFTEST=<dir> AgentWatch          drive the real UI from <dir>/cmd (see SelfTest.swift)
@main
enum Main {
    static func main() {
        let args = CommandLine.arguments
        if args.contains("--selftest-crypto") { exit(CryptoSelfTest.run()) }
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            MainActor.assumeIsolated { Snapshotter.run(out: args[i + 1], args: args) }
            return
        }
        let app = NSApplication.shared
        if ProcessInfo.processInfo.environment["AGENTWATCH_HARNESS"] != nil {
            // test harness: no menu bar item, just the panel driven by SelfTest
            app.setActivationPolicy(.prohibited)
            MainActor.assumeIsolated {
                let store = Store()
                let peers = PeerService(store: store)
                Harness.store = store
                Harness.peers = peers
                peers.start()
                SelfTest.startHarness(store: store, peers: peers)
                SelfTest.start()
            }
            app.run()
            return
        }
        let delegate = MainActor.assumeIsolated { AppDelegate() }
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

@MainActor enum Harness { static var store: Store?; static var peers: PeerService? }

/// Owns the status item and its popover. A plain NSStatusItem (rather than SwiftUI's MenuBarExtra) so the window
/// can be opened from code: by the self-test, and by a click on a notification.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    static private(set) var shared: AppDelegate?
    let store = Store()
    private(set) var peers: PeerService!
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private var watch: AnyCancellable?
    private var resized: [NSObjectProtocol] = []

    func applicationDidFinishLaunching(_ note: Notification) {
        AppDelegate.shared = self
        peers = PeerService(store: store)
        Harness.peers = peers
        peers.start()
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
        popover.contentViewController = NSHostingController(rootView: Popover().environmentObject(store).environmentObject(peers))
        item.button?.target = self
        item.button?.action = #selector(toggle)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        watch = store.$snap.combineLatest(peers.$unread).sink { [weak self] s, unread in
            self?.item.button?.image = StatusIcon.make(asks: s.asks.count, paused: s.pausedUntil != nil, running: s.cards.contains { $0.running },
                                                       messages: unread.values.reduce(0, +))
            self?.item.button?.toolTip = s.asks.isEmpty ? "Agent Watch" : "\(s.asks.count) agent\(s.asks.count == 1 ? "" : "s") need you"
        }
        SelfTest.start()
    }

    var isOpen: Bool { popover.isShown }

    @objc func toggle() {
        if NSApp.currentEvent?.type == .rightMouseUp { showMenu(); return }
        isOpen ? close() : open()
    }

    func open() {
        guard let button = item.button else { return }
        store.reload()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        // The list measures itself after the popover opens, and a window grows from its bottom-left corner, so the
        // popover would grow upwards past the menu bar and hide its header. Keep its top pinned under the icon.
        resized.forEach(NotificationCenter.default.removeObserver)
        let window = popover.contentViewController?.view.window
        resized = [NSWindow.didResizeNotification, NSWindow.didMoveNotification].map { name in
            NotificationCenter.default.addObserver(forName: name, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.pinUnderIcon() }
            }
        }
        pinUnderIcon()
        NSApp.activate()
    }

    func close() { popover.performClose(nil) }

    /// Puts the popover's top edge just under the menu bar icon.
    private func pinUnderIcon() {
        guard popover.isShown, let w = popover.contentViewController?.view.window, let bar = item.button?.window else { return }
        let top = bar.frame.minY
        if abs(w.frame.maxY - top) > 0.5 { w.setFrameTopLeftPoint(NSPoint(x: w.frame.minX, y: top)) }
    }

    func popoverDidShow(_ note: Notification) { pinUnderIcon() }

    func popoverDidClose(_ note: Notification) {
        resized.forEach(NotificationCenter.default.removeObserver)
        resized = []
    }

    var popoverWindow: NSWindow? { popover.isShown ? popover.contentViewController?.view.window : nil }

    private func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "Open Paseo", action: #selector(openPaseo), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Agent Watch", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = menu
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc private func openPaseo() { store.openPaseo() }
    @objc private func openDashboard() { store.openDashboard() }
}

/// The menu bar icon: a red pill with the count when agents need you, otherwise a quiet template glyph.
enum StatusIcon {
    static func make(asks: Int, paused: Bool, running: Bool, messages: Int = 0) -> NSImage {
        if asks > 0 { return pill(count: asks) }
        if messages > 0 { return pill(count: messages, color: .systemBlue, symbol: "bubble.left.fill") }
        let name = paused ? "moon.zzz.fill" : (running ? "antenna.radiowaves.left.and.right" : "antenna.radiowaves.left.and.right.circle")
        let config = NSImage.SymbolConfiguration(pointSize: 13.5, weight: .semibold)
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "Agent Watch")!.withSymbolConfiguration(config)!
        img.isTemplate = true
        return img
    }

    private static func pill(count: Int, color: NSColor = .systemRed, symbol name: String = "person.fill.questionmark") -> NSImage {
        let text = "\(count)" as NSString
        let font = NSFont.systemFont(ofSize: 11, weight: .bold)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
        let textSize = text.size(withAttributes: attrs)
        let symbol = NSImage(systemSymbolName: name, accessibilityDescription: nil)!
            .withSymbolConfiguration(.init(pointSize: 10.5, weight: .bold))!
        let width = 8 + symbol.size.width + 3 + textSize.width + 8
        let size = NSSize(width: ceil(width), height: 17)
        let img = NSImage(size: size, flipped: false) { rect in
            color.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 8.5, yRadius: 8.5).fill()
            let tinted = symbol.copy() as! NSImage
            tinted.lockFocus()
            NSColor.white.set()
            NSRect(origin: .zero, size: tinted.size).fill(using: .sourceAtop)
            tinted.unlockFocus()
            tinted.draw(in: NSRect(x: 8, y: (rect.height - symbol.size.height) / 2, width: symbol.size.width, height: symbol.size.height))
            text.draw(at: NSPoint(x: 8 + symbol.size.width + 3, y: (rect.height - textSize.height) / 2), withAttributes: attrs)
            return true
        }
        img.isTemplate = false
        img.accessibilityDescription = "\(count) agents need you"
        return img
    }
}

/// Renders the window offscreen to a PNG so its look can be checked without clicking the menu bar.
enum Snapshotter {
    @MainActor
    static func run(out: String, args: [String]) {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        func value(_ flag: String) -> String? {
            guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
            return args[i + 1]
        }
        let root = value("--root").map { URL(fileURLWithPath: $0) } ?? Store.findRoot()
        let store = Store(root: root, live: false)
        let maxScroll = CGFloat(Double(value("--height") ?? "") ?? 560)
        let view = Popover(maxScroll: maxScroll).environmentObject(store).environmentObject(PeerService(store: store))
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingView(rootView: view)
        let dark = args.contains("--dark")
        host.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 400, height: 900), styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = host.appearance
        window.contentView = host
        // two layout passes: the scroll area sizes itself from its content's measured height
        for _ in 0..<3 {
            host.layoutSubtreeIfNeeded()
            RunLoop.main.run(until: Date().addingTimeInterval(0.25))
            window.setContentSize(host.fittingSize)
        }
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
        host.cacheDisplay(in: host.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: out))
        print("wrote \(out) \(Int(host.bounds.width))x\(Int(host.bounds.height))")
        exit(0)
    }
}
