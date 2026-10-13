import AppKit
import CryptoKit
import Network
import SwiftUI

/// End-to-end test driver. With AGENTWATCH_SELFTEST=<dir> set, the running app reads commands from <dir>/cmd and
/// acts on its own UI through the accessibility tree (the same path VoiceOver uses), so tests click the real buttons
/// in the real menu bar window without needing accessibility permission for another process.
///   open | close                    show / hide the menu bar popover
///   press <label>                   press the first button whose label or title contains <label>
///   type <placeholder>|<text>       type into the text field whose placeholder contains <placeholder>
///   dump                            write every labelled element to <dir>/tree.txt
/// Each command's outcome is appended to <dir>/result.
@MainActor
enum SelfTest {
    /// A focus-free stand-in for the popover: a non-activating panel hosting the same view and store, so tests can
    /// click and type without taking focus from whatever the user is doing.
    static func startHarness(store: Store, peers: PeerService) {
        let panel = HarnessPanel(contentRect: NSRect(x: 40, y: 40, width: 400, height: 1500),
                                 styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView], backing: .buffered, defer: false)
        panel.titlebarAppearsTransparent = true
        panel.titleVisibility = .hidden
        panel.isFloatingPanel = false
        panel.level = .normal
        // tall, so test screenshots show the whole popover without scrolling
        panel.contentViewController = NSHostingController(rootView: Popover(maxScroll: 1400).environmentObject(store).environmentObject(peers))
        panel.setFrameOrigin(NSPoint(x: -3000, y: 40))   // offscreen: rendered and clickable, never in the user's way
        panel.orderFrontRegardless()
        // not key: a key panel would take the user's real keystrokes. It becomes key only while a command types.
        harness = panel
    }

    static func start() {
        guard let dir = ProcessInfo.processInfo.environment["AGENTWATCH_SELFTEST"] else { return }
        let cmd = URL(fileURLWithPath: dir).appendingPathComponent("cmd")
        let result = URL(fileURLWithPath: dir).appendingPathComponent("result")
        Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in
            MainActor.assumeIsolated {
                guard let text = try? String(contentsOf: cmd, encoding: .utf8), !text.isEmpty else { return }
                try? "".write(to: cmd, atomically: true, encoding: .utf8)
                for line in text.split(separator: "\n") {
                    let out = run(String(line), dir: dir)
                    if let h = try? FileHandle(forWritingTo: result) {
                        h.seekToEndOfFile(); h.write("\(line) => \(out)\n".data(using: .utf8)!); try? h.close()
                    } else {
                        try? "\(line) => \(out)\n".write(to: result, atomically: true, encoding: .utf8)
                    }
                }
            }
        }
    }

    static func run(_ line: String, dir: String) -> String {
        let (verb, arg) = line.split(separator: " ", maxSplits: 1).map(String.init).splat()
        switch verb {
        case "open":
            AppDelegate.shared?.open(); return "ok"
        case "close":
            AppDelegate.shared?.close(); return "ok"
        case "click":
            // click <x> <y>: points from the window's top-left corner
            guard let w = popover() else { return "window not open" }
            let xy = arg.split(separator: " ").compactMap { Double($0) }
            guard xy.count == 2 else { return "usage: click <x> <y>" }
            let p = NSPoint(x: xy[0], y: w.frame.height - xy[1])
            let wasKey = w.isKeyWindow
            if w === harness { w.makeKey() }
            defer { if w === harness && !wasKey { w.resignKey() } }
            for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                if let e = NSEvent.mouseEvent(with: type, location: p, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                              windowNumber: w.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1) {
                    w.sendEvent(e)
                }
            }
            return "clicked \(Int(xy[0])),\(Int(xy[1]))"
        case "keys":
            // keys <text>: typed into whatever has focus; "\\r" is Return
            guard let w = popover() else { return "window not open" }
            let text = arg.replacingOccurrences(of: "\\r", with: "\r")
            if w === harness { w.makeKey() }
            defer { if w === harness { w.resignKey() } }
            for ch in text {
                for type in [NSEvent.EventType.keyDown, .keyUp] {
                    let code: UInt16 = ch == "\r" ? 36 : 0
                    if let e = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                                windowNumber: w.windowNumber, context: nil, characters: String(ch),
                                                charactersIgnoringModifiers: String(ch), isARepeat: false, keyCode: code) {
                        w.sendEvent(e)
                    }
                }
            }
            return "typed \(text.count) keys"
        case "shot":
            // shot <file.png>: the window's content as rendered right now
            guard let v = popover()?.contentView, let rep = v.bitmapImageRepForCachingDisplay(in: v.bounds) else { return "window not open" }
            v.cacheDisplay(in: v.bounds, to: rep)
            try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: arg))
            return "saved \(Int(v.bounds.width))x\(Int(v.bounds.height))"
        case "press":
            guard let root = popover() else { return "window not open" }
            guard let el = find(root, { role($0) == NSAccessibility.Role.button.rawValue && label($0).localizedCaseInsensitiveContains(arg) })
            else { return "no button \(arg)" }
            (el as AnyObject).accessibilityPerformAction?(NSAccessibility.Action.press)
            return "pressed \(label(el))"
        case "type":
            guard let root = popover() else { return "window not open" }
            let parts = arg.split(separator: "|", maxSplits: 1).map(String.init)
            guard parts.count == 2 else { return "usage: type <placeholder>|<text>" }
            guard let el = find(root, { isText($0) && placeholder($0).localizedCaseInsensitiveContains(parts[0]) })
            else { return "no field \(parts[0])" }
            (el as AnyObject).accessibilitySetValue?(parts[1], forAttribute: .value)
            return "typed into \(placeholder(el))"
        case "frame":
            // screen rectangle in screencapture's coordinates (points, top-left origin)
            guard let w = popover(), let screen = w.screen ?? NSScreen.main else { return "window not open" }
            let f = w.frame
            return "\(Int(f.minX)),\(Int(screen.frame.maxY - f.maxY)),\(Int(f.width)),\(Int(f.height))"
        case "pair":
            // pair <host:port> <name>: start linking with another instance without Bonjour
            let parts = arg.split(separator: " ", maxSplits: 1).map(String.init)
            guard let peers = Harness.peers, let ep = PeerService.endpoint(parts.first ?? "") else { return "usage: pair host:port name" }
            peers.startPairing(endpoint: ep, name: parts.count > 1 ? parts[1] : "peer"); return "pairing"
        case "confirm":
            Harness.peers?.confirmPairing(); return "confirmed"
        case "peers":
            guard let p = Harness.peers else { return "no peer service" }
            let code = p.pairing?.code ?? "-"
            return "me=\(p.myName)/\(p.identity.id) code=\(code) linked=\(p.linkedCount) peers=" + p.peers.map { "\($0.name)/\($0.id)/\($0.share)/\(p.online.contains($0.id) ? "on" : "off")" }.joined(separator: ",")
        case "windows":
            return NSApp.windows.map { "\(type(of: $0)) visible=\($0.isVisible) \(Int($0.frame.minX)),\(Int($0.frame.minY)) \(Int($0.frame.width))x\(Int($0.frame.height))" }.joined(separator: "; ")
        case "dump":
            guard let root = popover() else { return "window not open" }
            var lines: [String] = []
            walk(root, depth: 0) { el, d in
                let l = label(el), p = placeholder(el)
                if !l.isEmpty || !p.isEmpty { lines.append(String(repeating: "  ", count: d) + "\(role(el)) \(l)\(p.isEmpty ? "" : " [\(p)]")") }
            }
            try? lines.joined(separator: "\n").write(toFile: dir + "/tree.txt", atomically: true, encoding: .utf8)
            return "\(lines.count) elements"
        default:
            return "unknown command"
        }
    }

    // MARK: finding things

    /// The window under test: the harness panel, or else the real popover.
    static var harness: NSWindow?
    static func popover() -> NSWindow? { harness ?? AppDelegate.shared?.popoverWindow }

    static func firstView<T: NSView>(_ v: NSView?, _ t: T.Type) -> T? {
        guard let v else { return nil }
        if let m = v as? T { return m }
        for s in v.subviews { if let m = firstView(s, t) { return m } }
        return nil
    }

    static func attr(_ el: Any, _ a: NSAccessibility.Attribute) -> Any? { (el as AnyObject).accessibilityAttributeValue?(a) }
    static func role(_ el: Any) -> String { attr(el, .role) as? String ?? "" }
    /// A button's name: its own label, or else the text inside it (SwiftUI puts a button's text in child elements).
    static func label(_ el: Any) -> String {
        if let own = [attr(el, .description), attr(el, .title)].compactMap({ $0 as? String }).first(where: { !$0.isEmpty }) { return own }
        var texts: [String] = []
        walk(el, depth: 0) { c, _ in
            if role(c) == NSAccessibility.Role.staticText.rawValue, let v = attr(c, .value) as? String, !v.isEmpty { texts.append(v) }
        }
        return texts.joined(separator: " ")
    }
    static func placeholder(_ el: Any) -> String { attr(el, .placeholderValue) as? String ?? "" }
    static func isText(_ el: Any) -> Bool {
        [NSAccessibility.Role.textField.rawValue, NSAccessibility.Role.textArea.rawValue].contains(role(el))
    }

    static func walk(_ el: Any, depth: Int, _ visit: (Any, Int) -> Void) {
        visit(el, depth)
        for c in (attr(el, .children) as? [Any]) ?? [] { walk(c, depth: depth + 1, visit) }
    }

    static func find(_ el: Any, _ match: (Any) -> Bool) -> Any? {
        if match(el) { return el }
        for c in (attr(el, .children) as? [Any]) ?? [] { if let m = find(c, match) { return m } }
        return nil
    }
}

private extension Array where Element == String {
    func splat() -> (String, String) { (first ?? "", count > 1 ? self[1] : "") }
}

final class HarnessPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// `AgentWatch --selftest-crypto`: checks the teammate-link cryptography and exits non-zero on any failure.
enum CryptoSelfTest {
    static func run() -> Int32 {
        var failures = 0
        func check(_ ok: Bool, _ what: String) { print(ok ? "ok   \(what)" : "FAIL \(what)"); if !ok { failures += 1 } }
        let dirA = FileManager.default.temporaryDirectory.appendingPathComponent("aw-ct-\(UUID().uuidString)")
        let dirB = FileManager.default.temporaryDirectory.appendingPathComponent("aw-ct-\(UUID().uuidString)")
        let a = Identity.load(dir: dirA), b = Identity.load(dir: dirB)
        check(Identity.load(dir: dirA).id == a.id, "identity persists across loads")
        let perms = (try? FileManager.default.attributesOfItem(atPath: dirA.appendingPathComponent("identity.key").path))?[.posixPermissions] as? Int
        check(perms == 0o600, "identity key file is 0600")
        // pairing: both sides derive the same code; a different nonce or key changes it
        let nI = PeerCrypto.random(), nR = PeerCrypto.random()
        let codeA = PeerCrypto.sas(idI: a.publicKey, idR: b.publicKey, nonceI: nI, nonceR: nR)
        let codeB = PeerCrypto.sas(idI: a.publicKey, idR: b.publicKey, nonceI: nI, nonceR: nR)
        check(codeA == codeB && codeA.count == 7, "both sides show the same 6-digit code (\(codeA))")
        let mallory = Identity.load(dir: FileManager.default.temporaryDirectory.appendingPathComponent("aw-ct-\(UUID().uuidString)"))
        check(PeerCrypto.sas(idI: a.publicKey, idR: mallory.publicKey, nonceI: nI, nonceR: nR) != codeA, "a swapped identity key changes the code")
        check(PeerCrypto.sha256(nI) != PeerCrypto.sha256(PeerCrypto.random()), "commitment rejects a different nonce")
        // codes are spread evenly enough that guessing one is 1 in a million
        var seen = Set<String>()
        for _ in 0..<2000 { seen.insert(PeerCrypto.sas(idI: a.publicKey, idR: b.publicKey, nonceI: PeerCrypto.random(), nonceR: PeerCrypto.random())) }
        check(seen.count > 1990, "codes don't repeat in 2000 pairings (\(seen.count) distinct)")
        // session: signatures bind the transcript; keys agree; frames can't be altered, replayed or reordered
        let eC = Curve25519.KeyAgreement.PrivateKey(), eS = Curve25519.KeyAgreement.PrivateKey()
        let t = PeerCrypto.transcript(idC: a.publicKey, idS: b.publicKey, eC: eC.publicKey.rawRepresentation, eS: eS.publicKey.rawRepresentation)
        let sig = try! b.sign(t + Data("server".utf8))
        check(Identity.verify(sig, t + Data("server".utf8), publicKey: b.publicKey), "server signature verifies")
        check(!Identity.verify(sig, t + Data("server".utf8), publicKey: mallory.publicKey), "signature fails against the wrong key")
        check(!Identity.verify(sig, t + Data("client".utf8), publicKey: b.publicKey), "a server signature can't be reused as the client's")
        let kc = PeerCrypto.sessionKeys(try! eC.sharedSecretFromKeyAgreement(with: eS.publicKey), transcript: t)
        let ks = PeerCrypto.sessionKeys(try! eS.sharedSecretFromKeyAgreement(with: eC.publicKey), transcript: t)
        let client = SecureChannel(send: kc.c2s, receive: kc.s2c), server = SecureChannel(send: ks.s2c, receive: ks.c2s)
        let f1 = try! client.seal(Data("one".utf8)), f2 = try! client.seal(Data("two".utf8))
        check((try? server.open(f1)) == Data("one".utf8), "frame opens on the other side")
        var bad = f2; bad[bad.startIndex] ^= 1
        check((try? server.open(bad)) == nil, "an altered frame is rejected")
        check((try? server.open(f1)) == nil, "a replayed frame is rejected")
        let s2 = SecureChannel(send: ks.s2c, receive: ks.c2s)
        check((try? s2.open(f2)) == nil, "an out-of-order frame is rejected")
        check(f1.range(of: Data("one".utf8)) == nil, "frames are not plaintext")
        // local-only rule
        check(LocalOnly.allowed(.hostPort(host: "192.168.1.20", port: 1)) && LocalOnly.allowed(.hostPort(host: "10.0.0.5", port: 1))
              && LocalOnly.allowed(.hostPort(host: "fe80::1", port: 1)) && LocalOnly.allowed(.hostPort(host: "127.0.0.1", port: 1)),
              "private and link-local addresses are allowed")
        check(!LocalOnly.allowed(.hostPort(host: "8.8.8.8", port: 1)) && !LocalOnly.allowed(.hostPort(host: "2001:4860::8888", port: 1))
              && !LocalOnly.allowed(.hostPort(host: "172.32.0.1", port: 1)), "internet addresses are refused")
        for d in [dirA, dirB] { try? FileManager.default.removeItem(at: d) }
        print(failures == 0 ? "all crypto checks passed" : "\(failures) checks failed")
        return failures == 0 ? 0 : 1
    }
}
