// Accessibility "hands" for Human Emulator: read another app's UI and act on it.
// Everything goes through Apple's Accessibility API on this Mac.
//
// Usage:
//   case-ax trusted [--prompt]                       -> {"trusted":true|false}; --prompt shows macOS's dialog
//   case-ax screen  [--prompt]                       -> {"granted":true|false} for Screen Recording
//   case-ax front                                    -> {"bundle_id": …} of the frontmost app
//   case-ax under <bundle-id>                        -> {"bundle_id", "name"} of the app right behind that one
//   case-ax press-seq <bundle-id> <gap-ms> <label>…  -> walk once, press each named control in order ("A|B" = either)
//   case-ax press-label <bundle-id> <label>          -> paper's AX tier: press any control by its name (warm-up
//                                                       re-walk when the tree is thin); {"found": false, …} if absent
//   case-ax vision-click <bundle-id> <text>          -> paper's OCR tier: read the front window with Apple Vision
//                                                       and click the best-matching text; image never leaves the Mac
//   case-ax vision-read <bundle-id>                  -> the text Apple Vision reads in the app's front window
//   case-ax dump  <bundle-id> [ROLE,ROLE…]           -> JSON array of UI elements in reading order
//   case-ax press <bundle-id> <role> <label> [last]  -> press the element whose label matches exactly
//   case-ax press-name <bundle-id> <role> <name>     -> press the first row whose name (label before
//                                                       the first comma) matches, e.g. "Ridhi, 2 unread messages"
//   case-ax match <bundle-id> <role> <name>          -> {"exact": n, "name": n} — counts only, presses nothing
//   case-ax focus <bundle-id> <role> <label> [last]  -> give that element keyboard focus
//   case-ax read  <bundle-id> <role> <label> [last]  -> {"value": …} of that element
//   case-ax type  <bundle-id> <text>                 -> type text as Unicode key events (no clipboard)
//   case-ax key   <bundle-id> paste|return|selectall|delete|escape
//
// Labels are compared after removing invisible bidi marks, collapsing spaces and
// ignoring case. Key presses are posted to the target app's process only, so they
// can never land in whatever other window happens to be in front.

import AppKit
import ApplicationServices
import Foundation
import Vision

struct Node: Encodable {
    let i: Int
    let role: String
    let label: String
    let value: String
    let focused: Bool
}

struct FoundNothing: Encodable { let found: Bool; let elements: Int; let warmed: Bool; let similar: [String] }
struct Pressed: Encodable { let found: Bool; let label: String; let role: String; let elements: Int; let warmed: Bool }
struct Missing: Encodable { let found: Bool; let missing: String; let elements: Int }
struct Sequence: Encodable { let found: Bool; let pressed: Int; let elements: Int; let warmed: Bool; let ms: Int }
struct Read: Encodable { let lines: [String]; let ms: Int }
struct NotSeen: Encodable { let found: Bool; let lines: Int; let seen: [String]; let ms: Int }
struct Clicked: Encodable {
    let found: Bool; let match: String; let confidence: Double; let x: Int; let y: Int; let lines: Int; let ms: Int
}

func emit<T: Encodable>(_ value: T) -> Never {
    FileHandle.standardOutput.write(try! JSONEncoder().encode(value))
    exit(0)
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func normalize(_ text: String) -> String {
    let invisible: Set<UInt32> = [0x200B, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF]
    let kept = String(String.UnicodeScalarView(text.unicodeScalars.filter { !invisible.contains($0.value) }))
    let edges = CharacterSet.punctuationCharacters.union(.whitespacesAndNewlines)
    return kept.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ").trimmingCharacters(in: edges).lowercased()
}

func copy(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

func text(_ element: AXUIElement, _ name: String) -> String {
    copy(element, name) as? String ?? ""
}

func label(_ element: AXUIElement) -> String {
    let description = text(element, kAXDescriptionAttribute)
    return description.isEmpty ? text(element, kAXTitleAttribute) : description
}

func process(_ bundleID: String) -> pid_t {
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
        fail("not_running: \(bundleID) is not running", code: 3)
    }
    return app.processIdentifier
}

func walk(_ pid: pid_t, limit: Int = 6000) -> [(AXUIElement, Node)] {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 3)
    var roots = (copy(app, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    if roots.isEmpty, let focused = copy(app, kAXFocusedWindowAttribute) {
        roots = [focused as! AXUIElement]
    }
    var stack = Array(roots.reversed())
    var out: [(AXUIElement, Node)] = []
    while let element = stack.popLast(), out.count < limit {
        let value = copy(element, kAXValueAttribute) as? String ?? ""
        let node = Node(
            i: out.count,
            role: text(element, kAXRoleAttribute),
            label: label(element),
            value: String(value.prefix(400)),
            focused: (copy(element, kAXFocusedAttribute) as? Bool) ?? false
        )
        out.append((element, node))
        let children = (copy(element, kAXChildrenAttribute) as? [AXUIElement]) ?? []
        stack.append(contentsOf: children.reversed())
    }
    return out
}

func find(_ pid: pid_t, role: String, wanted: String, last: Bool, byName: Bool = false) -> (AXUIElement, Node) {
    let matches = matching(pid, role: role, wanted: wanted, byName: byName)
    guard let match = last ? matches.last : matches.first else {
        fail("not_found: no \(role) labelled “\(wanted)”", code: 4)
    }
    return match
}

// A list row is often labelled "Name, 2 unread messages, …": its name is the part
// before the first comma. Callers must still verify what opened.
func matching(_ pid: pid_t, role: String, wanted: String, byName: Bool) -> [(AXUIElement, Node)] {
    let target = normalize(wanted)
    return walk(pid).filter { element in
        guard element.1.role == role else { return false }
        let label = byName ? String(element.1.label.split(separator: ",", maxSplits: 1).first ?? "") : element.1.label
        return normalize(label) == target
    }
}

func post(_ pid: pid_t, key: CGKeyCode, flags: CGEventFlags = []) {
    let source = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: down) else {
            fail("could not create key event")
        }
        event.flags = flags
        event.postToPid(pid)
        usleep(30_000)
    }
}

// Types text as Unicode key events straight into the app — the clipboard is never touched.
func type(_ pid: pid_t, _ text: String) {
    let source = CGEventSource(stateID: .hidSystemState)
    let units = Array(text.utf16)
    var start = 0
    while start < units.count {
        var end = min(start + 16, units.count)
        if end < units.count, UTF16.isLeadSurrogate(units[end - 1]) { end -= 1 }
        var chunk = Array(units[start..<end])
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else {
                fail("could not create key event")
            }
            event.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: &chunk)
            event.postToPid(pid)
        }
        usleep(15_000)
        start = end
    }
}

// MARK: - Any app: Accessibility first, on-device OCR as the fallback (paper Section IV-B)

func actionNames(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let list = names as? [String] else { return [] }
    return list
}

func bringForward(_ pid: pid_t, waitMs: UInt32 = 250) {
    NSRunningApplication(processIdentifier: pid)?.activate(options: [])
    usleep(waitMs * 1000)
}

// Chromium/Electron/WebKit apps build their tree lazily: a thin first walk (< 40 elements)
// gets an 800 ms wait and a second walk, and the larger one wins.
func warmWalk(_ pid: pid_t) -> (nodes: [(AXUIElement, Node)], warmed: Bool) {
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    var nodes = walk(pid)
    guard nodes.count < 40 else { return (nodes, false) }
    usleep(800_000)
    let retry = walk(pid)
    if retry.count >= nodes.count { nodes = retry }
    return (nodes, true)
}

func pressable(_ element: AXUIElement) -> Bool {
    actionNames(element).contains(kAXPressAction as String)
}

func frontWindow(_ pid: pid_t) -> (id: Int, bounds: CGRect)? {
    guard let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { return nil }
    for window in info {
        guard (window[kCGWindowOwnerPID as String] as? Int32) == pid,
              (window[kCGWindowLayer as String] as? Int) == 0,
              let raw = window[kCGWindowBounds as String] as? [String: Any],
              let rect = CGRect(dictionaryRepresentation: raw as CFDictionary),
              rect.width > 80, rect.height > 80,
              let id = window[kCGWindowNumber as String] as? Int else { continue }
        return (id, rect)
    }
    return nil
}

struct SeenLine {
    let text: String
    let confidence: Float
    let box: CGRect  // image pixels, top-left origin
    let candidate: VNRecognizedText
}

func readWindow(_ pid: pid_t) -> (lines: [SeenLine], bounds: CGRect, scale: CGFloat, ms: Int) {
    guard CGPreflightScreenCaptureAccess() else {
        fail("not_allowed: Screen Recording is off for the app running Human Emulator", code: 5)
    }
    // Stage Manager and Spaces animate the window into place after activation; wait for a full-size window.
    var found = frontWindow(pid)
    for _ in 0..<16 where (found?.bounds.width ?? 0) < 200 {
        usleep(150_000)
        found = frontWindow(pid)
    }
    guard let (id, bounds) = found else { fail("no_window: the app has no window on screen", code: 6) }
    let path = NSTemporaryDirectory() + "case-ax-vision-\(pid).png"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let started = Date()
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-l", String(id), path]
    try? capture.run()
    capture.waitUntilExit()
    guard capture.terminationStatus == 0, let image = NSImage(contentsOfFile: path),
          let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        fail("window capture failed")
    }
    let width = CGFloat(cg.width), height = CGFloat(cg.height)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    let lines = (request.results ?? []).compactMap { observation -> SeenLine? in
        guard let top = observation.topCandidates(1).first else { return nil }
        let bb = observation.boundingBox
        let box = CGRect(x: bb.minX * width, y: (1 - bb.maxY) * height, width: bb.width * width, height: bb.height * height)
        return SeenLine(text: top.string, confidence: top.confidence, box: box, candidate: top)
    }
    return (lines, bounds, width / max(bounds.width, 1), Int(Date().timeIntervalSince(started) * 1000))
}

// Paper scoring: 3 exact, 2.5 whole word, 2 substring, plus 0.1 × recognition confidence.
func bestMatch(_ lines: [SeenLine], _ wanted: String) -> SeenLine? {
    let want = normalize(wanted)
    guard !want.isEmpty else { return nil }
    var best: (Double, SeenLine)?
    for line in lines {
        let have = normalize(line.text)
        if have.isEmpty { continue }
        var score = 0.0
        if have == want { score = 3 }
        else if have.split(separator: " ").map(String.init).contains(want) { score = 2.5 }
        else if have.contains(want) { score = 2 }
        guard score > 0 else { continue }
        score += Double(line.confidence) * 0.1
        if score > (best?.0 ?? 0) { best = (score, line) }
    }
    return best?.1
}

func clickAt(_ point: CGPoint) {
    let source = CGEventSource(stateID: .hidSystemState)
    CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?
        .post(tap: .cghidEventTap)
    usleep(60_000)
    for type in [CGEventType.leftMouseDown, .leftMouseUp] {
        let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: .left)
        event?.setIntegerValueField(.mouseEventClickState, value: 1)
        event?.post(tap: .cghidEventTap)
        usleep(50_000)
    }
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: case-ax trusted|screen|front|dump|press|focus|read|key …") }

if args[1] == "trusted" {
    if args.count >= 3 && args[2] == "--prompt" {
        // Shows macOS's own "allow Accessibility" dialog and lists the host app in Settings.
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        emit(["trusted": AXIsProcessTrustedWithOptions(options)])
    }
    emit(["trusted": AXIsProcessTrusted()])
}
if args[1] == "screen" {
    if args.count >= 3 && args[2] == "--prompt" {
        emit(["granted": CGRequestScreenCaptureAccess()])
    }
    emit(["granted": CGPreflightScreenCaptureAccess()])
}
if args[1] == "front" {
    emit(["bundle_id": NSWorkspace.shared.frontmostApplication?.bundleIdentifier ?? ""])
}
if args[1] == "under" {
    // The app whose window sits right behind <exclude> (e.g. the browser showing the dashboard).
    let exclude = args.count >= 3 ? args[2] : ""
    let skip: Set<String> = [exclude, "com.apple.dock", "com.apple.WindowManager", "com.apple.controlcenter",
                             "com.apple.systemuiserver", "com.apple.notificationcenterui", "com.apple.Spotlight"]
    let me = ProcessInfo.processInfo.processIdentifier
    // Visible windows first; then all windows front-to-back, because Stage Manager and full-screen apps keep
    // the previous app's window off this desktop (describe brings it forward before reading).
    for options: CGWindowListOption in [[.optionOnScreenOnly, .excludeDesktopElements], [.optionAll, .excludeDesktopElements]] {
        let windows = (CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]]) ?? []
        for window in windows {
            guard (window[kCGWindowLayer as String] as? Int) == 0,
                  (window[kCGWindowAlpha as String] as? Double ?? 1) > 0,
                  let owner = window[kCGWindowOwnerPID as String] as? Int32, owner != me,
                  let app = NSRunningApplication(processIdentifier: owner), app.activationPolicy == .regular,
                  let bundle = app.bundleIdentifier, !skip.contains(bundle),
                  let raw = window[kCGWindowBounds as String] as? [String: Any],
                  let rect = CGRect(dictionaryRepresentation: raw as CFDictionary),
                  rect.width > 120, rect.height > 120 else { continue }
            emit(["bundle_id": bundle, "name": app.localizedName ?? bundle])
        }
    }
    emit(["bundle_id": "", "name": ""])
}
if args[1] == "vision-warmup" {
    // Apple Vision loads its text models on first use (up to a minute); do it before the demo, not during it.
    let size = NSSize(width: 360, height: 90)
    let image = NSImage(size: size)
    image.lockFocus()
    NSColor.white.setFill()
    NSRect(origin: .zero, size: size).fill()
    ("Warm up" as NSString).draw(at: NSPoint(x: 16, y: 26), withAttributes: [.font: NSFont.systemFont(ofSize: 34)])
    image.unlockFocus()
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    if let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
    }
    emit(["ready": !(request.results ?? []).isEmpty])
}
guard AXIsProcessTrusted() else {
    fail("not_trusted: Accessibility access is off for the app running Human Emulator", code: 2)
}
guard args.count >= 3 else { fail("missing bundle id") }
let pid = process(args[2])

switch args[1] {
case "dump":
    let roles = args.count >= 4 ? Set(args[3].split(separator: ",").map(String.init)) : []
    emit(walk(pid).map(\.1).filter { roles.isEmpty || roles.contains($0.role) })
case "press", "focus", "read":
    guard args.count >= 5 else { fail("usage: case-ax \(args[1]) <bundle-id> <role> <label> [last]") }
    let (element, node) = find(pid, role: args[3], wanted: args[4], last: args.count >= 6 && args[5] == "last")
    switch args[1] {
    case "press":
        let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
        guard result == .success else { fail("press failed (\(result.rawValue)) on \(node.label)") }
        emit(["pressed": node.label])
    case "focus":
        let result = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        guard result == .success else { fail("focus failed (\(result.rawValue)) on \(node.label)") }
        emit(["focused": node.label])
    default:
        emit(["value": copy(element, kAXValueAttribute) as? String ?? ""])
    }
case "press-name":
    guard args.count >= 5 else { fail("usage: case-ax press-name <bundle-id> <role> <name>") }
    let (element, _) = find(pid, role: args[3], wanted: args[4], last: false, byName: true)
    let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard result == .success else { fail("press failed (\(result.rawValue))") }
    emit(["pressed": args[4]])
case "match":
    guard args.count >= 5 else { fail("usage: case-ax match <bundle-id> <role> <name>") }
    emit([
        "exact": matching(pid, role: args[3], wanted: args[4], byName: false).count,
        "name": matching(pid, role: args[3], wanted: args[4], byName: true).count,
    ])
case "type":
    guard args.count >= 4 else { fail("usage: case-ax type <bundle-id> <text>") }
    type(pid, args[3])
    emit(["typed": args[3].count])
case "key":
    guard args.count >= 4 else { fail("usage: case-ax key <bundle-id> paste|return|selectall|delete") }
    switch args[3] {
    case "paste": post(pid, key: 9, flags: .maskCommand)
    case "return": post(pid, key: 36)
    case "selectall": post(pid, key: 0, flags: .maskCommand)
    case "delete": post(pid, key: 51)
    case "escape": post(pid, key: 53)
    default: fail("unknown key \(args[3])")
    }
    emit(["key": args[3]])
case "press-seq":
    // Calculator-style key sequences: walk once, then press each control by name with a short gap.
    // "A|B" accepts either label. Presses target elements, not positions, so moving the window is harmless.
    guard args.count >= 5 else { fail("usage: case-ax press-seq <bundle-id> <gap-ms> <label>…") }
    bringForward(pid)
    let (nodes, warmed) = warmWalk(pid)
    let gap = UInt32(args[3]) ?? 250
    let labels = Array(args[4...])
    var targets: [AXUIElement] = []
    for label in labels {
        let options = Set(label.split(separator: "|").map { normalize(String($0)) })
        guard let hit = nodes.first(where: { options.contains(normalize($0.1.label)) && pressable($0.0) }) else {
            emit(Missing(found: false, missing: label, elements: nodes.count))
        }
        targets.append(hit.0)
    }
    let started = Date()
    for (index, element) in targets.enumerated() {
        let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
        guard result == .success else { fail("press failed (\(result.rawValue)) on \(labels[index])") }
        usleep(gap * 1000)
    }
    emit(Sequence(found: true, pressed: targets.count, elements: nodes.count, warmed: warmed,
                  ms: Int(Date().timeIntervalSince(started) * 1000)))
case "press-label":
    guard args.count >= 4 else { fail("usage: case-ax press-label <bundle-id> <label>") }
    bringForward(pid)
    let (nodes, warmed) = warmWalk(pid)
    let want = normalize(args[3])
    let named = nodes.filter { normalize($0.1.label) == want && pressable($0.0) }
    // "Start" also finds the only control called "Start timer"; two such controls means not guessing.
    let prefixed = named.isEmpty ? nodes.filter { normalize($0.1.label).hasPrefix(want + " ") && pressable($0.0) } : []
    guard let (element, node) = named.first ?? (prefixed.count == 1 ? prefixed.first : nil) else {
        let similar = Array(Set(nodes.map(\.1.label).filter { !$0.isEmpty && normalize($0).contains(want) })).prefix(5)
        emit(FoundNothing(found: false, elements: nodes.count, warmed: warmed, similar: Array(similar)))
    }
    let result = AXUIElementPerformAction(element, kAXPressAction as CFString)
    guard result == .success else { fail("press failed (\(result.rawValue)) on \(node.label)") }
    emit(Pressed(found: true, label: node.label, role: node.role, elements: nodes.count, warmed: warmed))
case "vision-click", "vision-read":
    bringForward(pid, waitMs: 300)
    let seen = readWindow(pid)
    if args[1] == "vision-read" {
        emit(Read(lines: seen.lines.map(\.text), ms: seen.ms))
    }
    guard args.count >= 4 else { fail("usage: case-ax vision-click <bundle-id> <text>") }
    guard let hit = bestMatch(seen.lines, args[3]) else {
        emit(NotSeen(found: false, lines: seen.lines.count, seen: Array(seen.lines.prefix(30).map(\.text)), ms: seen.ms))
    }
    var box = hit.box
    // Narrow to the word only when it sits inside a longer line ("Start timer"), and only if that box is sane.
    if normalize(hit.text) != normalize(args[3]),
       let range = hit.text.range(of: args[3], options: .caseInsensitive),
       let sub = try? hit.candidate.boundingBox(for: range)?.boundingBox {
        let width = seen.scale * seen.bounds.width, height = seen.scale * seen.bounds.height
        let narrowed = CGRect(x: sub.minX * width, y: (1 - sub.maxY) * height, width: sub.width * width, height: sub.height * height)
        if hit.box.insetBy(dx: -4, dy: -4).contains(CGPoint(x: narrowed.midX, y: narrowed.midY)) { box = narrowed }
    }
    // Image pixels → screen points: (x, y) = (x_w + u/k, y_w + v/k), k = Retina scale (paper Eq. 2).
    let point = CGPoint(x: seen.bounds.minX + box.midX / seen.scale, y: seen.bounds.minY + box.midY / seen.scale)
    clickAt(point)
    emit(Clicked(found: true, match: hit.text, confidence: Double(hit.confidence), x: Int(point.x), y: Int(point.y),
                 lines: seen.lines.count, ms: seen.ms))
default:
    fail("unknown command \(args[1])")
}
