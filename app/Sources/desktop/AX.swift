import AppKit
import ApplicationServices

/// Dostępność: kto jest na wierzchu (brama sekretów), panel Ustawień systemowych, i auto-Allow dla
/// okna zgody zdalnego debugowania Chrome/Brave (osobne żądanie bramy przeglądarki, nie toolsetu).
enum AX {
    static func attr(_ e: AXUIElement, _ a: String) -> AnyObject? {
        var v: AnyObject?
        return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil
    }
    static func role(_ e: AXUIElement) -> String { (attr(e, kAXRoleAttribute) as? String) ?? "" }
    static func subrole(_ e: AXUIElement) -> String { (attr(e, kAXSubroleAttribute) as? String) ?? "" }
    static func title(_ e: AXUIElement) -> String {
        (attr(e, kAXTitleAttribute) as? String) ?? (attr(e, kAXDescriptionAttribute) as? String) ?? ""
    }
    static func children(_ e: AXUIElement) -> [AXUIElement] { (attr(e, kAXChildrenAttribute) as? [AXUIElement]) ?? [] }

    /// Bundle id aplikacji na wierzchu i, dla Ustawień systemowych, tytuł okna (nazwa panelu).
    static func frontmost() -> (bundle: String, window: String) {
        guard let app = NSWorkspace.shared.frontmostApplication else { return ("", "") }
        let bundle = app.bundleIdentifier ?? ""
        var window = ""
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        if let win = attr(axApp, kAXFocusedWindowAttribute) as! AXUIElement? ?? children(axApp).first(where: { role($0) == "AXWindow" }) {
            window = title(win)
        }
        return (bundle, window)
    }

    /// Szuka w oknach danego pid arkusza "Allow remote debugging?" i wciska w nim przycisk "Allow".
    /// Zwraca liczbę znalezionych arkuszy (bez wciskania, gdy != 1) i czy wciśnięto.
    static func allowRemoteDebugging(pid: pid_t) -> (sheets: Int, pressed: Bool) {
        let app = AXUIElementCreateApplication(pid)
        var sheets: [AXUIElement] = []
        let deadline = Date().addingTimeInterval(2.5)
        func walk(_ e: AXUIElement, _ depth: Int) {
            if depth > 16 || Date() > deadline { return }
            let r = role(e)
            let t = title(e)
            // arkusz/dialog z tą dokładną etykietą; Chrome rysuje go jako AXSheet w oknie
            if (r == "AXSheet" || r == "AXDialog" || subrole(e) == "AXDialog"), t == "Allow remote debugging?" {
                sheets.append(e)
                return  // nie schodź głębiej: przyciski wciśniemy z tego węzła
            }
            for c in children(e) { walk(c, depth + 1) }
        }
        for w in children(app) where role(w) == "AXWindow" { walk(w, 0) }
        guard sheets.count == 1 else { return (sheets.count, false) }
        guard let button = findButton(sheets[0], named: "Allow", depth: 0) else { return (1, false) }
        let pressed = AXUIElementPerformAction(button, kAXPressAction as CFString) == .success
        return (1, pressed)
    }

    private static func findButton(_ e: AXUIElement, named: String, depth: Int) -> AXUIElement? {
        if depth > 8 { return nil }
        if role(e) == "AXButton", title(e).caseInsensitiveCompare(named) == .orderedSame { return e }
        for c in children(e) { if let f = findButton(c, named: named, depth: depth + 1) { return f } }
        return nil
    }

    static func trusted() -> Bool { AXIsProcessTrusted() }
}
