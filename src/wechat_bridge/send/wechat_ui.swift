// WeChat window helper for the send path. One command per run; prints one JSON object. It runs as a child of
// the bridge process, so Accessibility trust is the host app's (Claude.app). Never types a keystroke (the
// input method garbles them); all writes are AX value sets and real mouse clicks. Commands:
//   trusted | activate | state | open <name> | set_composer (text on stdin) | click_send
import ApplicationServices
import AppKit

func attr(_ e: AXUIElement, _ n: String) -> AnyObject? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, n as CFString, &v) == .success ? (v as AnyObject?) : nil
}
func children(_ e: AXUIElement) -> [AXUIElement] { (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }
func ident(_ e: AXUIElement) -> String { attr(e, kAXIdentifierAttribute) as? String ?? "" }
func frameOf(_ e: AXUIElement) -> CGRect? {
    guard let f = attr(e, "AXFrame"), CFGetTypeID(f) == AXValueGetTypeID() else { return nil }
    var r = CGRect.zero; AXValueGetValue(f as! AXValue, .cgRect, &r); return r
}
var visited = 0
func find(_ root: AXUIElement, _ depth: Int = 0, _ pred: (AXUIElement) -> Bool) -> AXUIElement? {
    if depth > 16 || visited > 6000 { return nil }
    visited += 1
    if pred(root) { return root }
    for k in children(root) { if let f = find(k, depth + 1, pred) { return f } }
    return nil
}
func findFresh(_ root: AXUIElement, _ pred: (AXUIElement) -> Bool) -> AXUIElement? { visited = 0; return find(root, 0, pred) }
func out(_ o: [String: Any]) -> Never {
    let d = try! JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])
    print(String(data: d, encoding: .utf8)!); exit(0)
}
func fail(_ code: String) -> Never { out(["error": code]) }

guard let command = CommandLine.arguments.dropFirst().first else { fail("usage") }
if command == "trusted" { out(["trusted": AXIsProcessTrusted()]) }
guard AXIsProcessTrusted() else { fail("no_accessibility") }
guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.xinWeChat").first else { fail("wechat_not_running") }
let ax = AXUIElementCreateApplication(app.processIdentifier)
func window() -> AXUIElement? { (attr(ax, kAXWindowsAttribute) as? [AXUIElement])?.first }
func isFront() -> Bool { NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier }
func run(_ path: String, _ args: [String]) { let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args; try? p.run(); p.waitUntilExit() }
let systemWide = AXUIElementCreateSystemWide()
/// Every mouse event is preceded by this check: WeChat frontmost and the element under the point belongs to WeChat.
func guardPoint(_ p: CGPoint) {
    guard isFront() else { fail("wechat_not_frontmost") }
    var hit: AXUIElement?
    guard AXUIElementCopyElementAtPosition(systemWide, Float(p.x), Float(p.y), &hit) == .success, let h = hit else { fail("target_obscured") }
    var pid: pid_t = 0
    AXUIElementGetPid(h, &pid)
    guard pid == app.processIdentifier else { fail("target_obscured") }
}
func moveMouse(_ p: CGPoint) { guardPoint(p); CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap) }
func mouseClick(_ p: CGPoint) {   // exactly one down/up pair
    moveMouse(p); usleep(60_000)
    guardPoint(p)   // re-check right before the press: focus can change during the pause
    for t in [CGEventType.leftMouseDown, .leftMouseUp] {
        CGEvent(mouseEventSource: nil, mouseType: t, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap); usleep(60_000)
    }
}
func str(_ e: AXUIElement?, _ a: String = kAXValueAttribute) -> String? { e.flatMap { attr($0, a) as? String } }

/// The Send control: exactly one AXButton titled 发送 right of the composer centre and just below its top edge.
/// Zero or several candidates mean the layout changed: no match.
func sendButton(_ w: AXUIElement, _ cf: CGRect) -> AXUIElement? {
    var found: [AXUIElement] = []
    visited = 0
    func walk(_ e: AXUIElement, _ depth: Int) {
        if depth > 16 || visited > 6000 { return }
        visited += 1
        if (attr(e, kAXRoleAttribute) as? String) == "AXButton", (str(e, kAXTitleAttribute) ?? "") == "发送", let f = frameOf(e),
           f.minX > cf.midX, f.minY >= cf.maxY - 20, f.minY < cf.maxY + 120, f.maxX <= cf.maxX + 40 { found.append(e) }
        for k in children(e) { walk(k, depth + 1) }
    }
    walk(w, 0)
    return found.count == 1 ? found[0] : nil
}

func chatState(_ w: AXUIElement) -> [String: Any]? {
    guard let label = findFresh(w, { ident($0) == "current_chat_name_label" }) else { return nil }
    let composer = findFresh(w) { ident($0) == "chat_input_field" }
    let send = composer.flatMap(frameOf).flatMap { sendButton(w, $0) }
    var bubbles: [String] = []
    if let list = findFresh(w, { ident($0) == "chat_message_list" }) {
        bubbles = children(list).filter { ident($0) == "chat_bubble_item_view" }.compactMap { str($0, kAXTitleAttribute) }
    }
    var r: [String: Any] = ["title": str(label) ?? NSNull(), "bubbles": bubbles]
    r["count_label"] = str(findFresh(w) { ident($0) == "current_chat_count_label" }) ?? NSNull()
    r["composer_text"] = composer.flatMap { str($0) } ?? NSNull()
    r["send_enabled"] = send.flatMap { attr($0, kAXEnabledAttribute) as? Bool } ?? NSNull()
    return r
}

switch command {
case "activate":
    if !isFront() { run("/usr/bin/open", ["-b", "com.tencent.xinWeChat"]) }
    for _ in 0..<20 { if isFront() { break }; usleep(100_000) }
    isFront() ? out(["ok": true]) : fail("wechat_not_frontmost")
case "state":
    guard let w = window() else { fail("no_window") }
    guard let s = chatState(w) else { out(["state": NSNull()]) }
    out(["state": s])
case "open":
    guard CommandLine.arguments.count == 3, let w = window() else { fail("usage") }
    let name = CommandLine.arguments[2]
    func list() -> AXUIElement? { findFresh(w) { ident($0) == "session_list" } }
    func row() -> AXUIElement? { findFresh(w) { ident($0) == "session_item_" + name } }
    // Any starting tab: when the chat list is not showing, click the left-bar tab buttons (real clicks, relative to the
    // window's own frame) until it appears. A chat list with no chat selected counts as showing.
    if list() == nil, let wf = frameOf(w) {
        let bar = (children(w).first).map { children($0).filter { (attr($0, kAXRoleAttribute) as? String) == "AXButton" && ident($0).isEmpty } } ?? []
        let tabs = bar.compactMap { b in frameOf(b).map { (b, $0) } }.filter { $0.1.minX - wf.minX < 80 && $0.1.minX >= wf.minX }.sorted { $0.1.minY < $1.1.minY }
        for (_, f) in tabs {
            mouseClick(CGPoint(x: f.midX, y: f.midY))
            usleep(UInt32((NSEvent.doubleClickInterval + 0.25) * 1_000_000))   // never two clicks inside the double-click interval
            if list() != nil { break }
        }
    }
    guard list() != nil else { fail("chat_pane_unavailable") }
    if let s0 = chatState(w), (s0["title"] as? String) == name { out(["ok": true]) }
    func scroll(_ lines: Int32) {
        guard let l = list(), let f = frameOf(l) else { return }
        let p = CGPoint(x: f.midX, y: f.midY)
        moveMouse(p); usleep(40_000)
        guardPoint(p)
        CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 1, wheel1: lines, wheel2: 0, wheel3: 0)?.post(tap: .cghidEventTap); usleep(120_000)
    }
    for _ in 0..<12 { scroll(30) }
    var steps = 0
    while row() == nil && steps < 80 { scroll(-4); steps += 1 }
    guard row() != nil else { fail("chat_not_in_list") }
    for _ in 0..<30 {   // bring the whole row inside the list, then click once
        guard let l = list(), let rw = row(), let lf = frameOf(l), let rf = frameOf(rw) else { fail("chat_not_in_list") }
        if rf.minY >= lf.minY && rf.maxY <= lf.maxY && rf.width > 0 { mouseClick(CGPoint(x: rf.midX, y: rf.midY)); out(["ok": true]) }
        scroll(rf.maxY > lf.maxY ? -1 : 1)
    }
    fail("chat_not_in_list")
case "set_composer":
    guard let w = window(), let c = findFresh(w, { ident($0) == "chat_input_field" }) else { fail("ui_unavailable") }
    let text = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) ?? ""
    AXUIElementSetAttributeValue(c, kAXValueAttribute as CFString, text as CFString) == .success ? out(["ok": true]) : fail("ui_unavailable")
case "click_send":
    // click_send <name> <direct|group>, text on stdin. Everything is re-verified in this same run, right before the click,
    // so a chat switch or a draft edit since the last read cannot send the wrong thing. Any `fail` here happens before the
    // click; once the click is made the only reply is ok.
    guard CommandLine.arguments.count == 4, let w = window() else { fail("usage") }
    let name = CommandLine.arguments[2], group = CommandLine.arguments[3] == "group"
    let text = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) ?? ""
    guard let s = chatState(w), (s["title"] as? String) == name, (s["count_label"] is String) == group,
          (s["composer_text"] as? String) == text, (s["send_enabled"] as? Bool) == true else { fail("state_changed") }
    guard let composer = findFresh(w, { ident($0) == "chat_input_field" }), let cf = frameOf(composer),
          let b = sendButton(w, cf), let bf = frameOf(b) else { fail("send_control_not_found") }
    mouseClick(CGPoint(x: bf.midX, y: bf.midY)); out(["ok": true])
default: fail("usage")
}
