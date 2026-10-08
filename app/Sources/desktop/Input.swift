import CoreGraphics
import Foundation

/// Mysz i klawiatura jako globalne zdarzenia CGEvent (uprawnienie Accessibility przypięte do podpisu).
/// Współrzędne są w punktach układu globalnego (lewy górny róg). Fokus zmienia się tylko jako naturalny
/// skutek kliknięcia w okno, nigdy nie wymuszamy aktywacji aplikacji.
enum Input {
    static let tap = CGEventTapLocation.cghidEventTap

    private static func post(_ event: CGEvent?) { event?.post(tap: tap) }
    private static func pause(_ seconds: Double) { if seconds > 0 { Thread.sleep(forTimeInterval: seconds) } }

    static func move(to p: CGPoint) {
        post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left))
    }

    /// Modyfikatory z chordu ("ctrl", "shift", "ctrl+shift") na flagi; nieznane tokeny pomijamy.
    static func flags(_ text: String?) -> CGEventFlags {
        guard let text, !text.isEmpty else { return [] }
        var f: CGEventFlags = []
        for token in text.split(whereSeparator: { $0 == "+" || $0 == " " }) {
            if let mod = Keys.modifiers[token.lowercased()] { f.insert(mod) }
        }
        return f
    }

    static func click(_ p: CGPoint, button: CGMouseButton, count: Int, modifiers: CGEventFlags) {
        let (down, up): (CGEventType, CGEventType)
        switch button {
        case .right: (down, up) = (.rightMouseDown, .rightMouseUp)
        case .center: (down, up) = (.otherMouseDown, .otherMouseUp)
        default: (down, up) = (.leftMouseDown, .leftMouseUp)
        }
        move(to: p)
        for n in 1...max(1, count) {
            for type in [down, up] {
                guard let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: button) else { continue }
                e.setIntegerValueField(.mouseEventClickState, value: Int64(n))
                if !modifiers.isEmpty { e.flags = modifiers }
                post(e)
            }
            pause(0.02)
        }
    }

    static func mouseDown(_ p: CGPoint, modifiers: CGEventFlags) {
        move(to: p)
        let e = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
        if !modifiers.isEmpty { e?.flags = modifiers }
        post(e)
    }

    static func mouseUp(_ p: CGPoint, modifiers: CGEventFlags) {
        let e = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
        if !modifiers.isEmpty { e?.flags = modifiers }
        post(e)
    }

    static func drag(from a: CGPoint, to b: CGPoint, modifiers: CGEventFlags) {
        mouseDown(a, modifiers: modifiers)
        pause(0.02)
        let steps = 12
        for i in 1...steps {
            let t = Double(i) / Double(steps)
            let p = CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t)
            let e = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDragged, mouseCursorPosition: p, mouseButton: .left)
            if !modifiers.isEmpty { e?.flags = modifiers }
            post(e)
            pause(0.012)
        }
        mouseUp(b, modifiers: modifiers)
    }

    static func scroll(_ p: CGPoint, dx: Int32, dy: Int32, modifiers: CGEventFlags) {
        move(to: p)
        guard let e = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0) else { return }
        e.location = p
        if !modifiers.isEmpty { e.flags = modifiers }
        post(e)
    }

    /// Pisze dosłowny tekst (idzie do bieżącego fokusu). Każdy znak jako para down/up z unicode.
    static func type(_ text: String) {
        for scalar in text.unicodeScalars {
            let units = Array(String(scalar).utf16)
            for down in [true, false] {
                guard let e = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down) else { continue }
                units.withUnsafeBufferPointer { buf in
                    e.keyboardSetUnicodeString(stringLength: buf.count, unicodeString: buf.baseAddress)
                }
                post(e)
            }
        }
    }

    enum KeyError: Error { case unknown(String) }

    /// Wciska chord raz: modyfikatory w dół, klawisz down+up, modyfikatory w górę.
    static func keyChord(_ text: String, repeatCount: Int = 1) throws {
        var mods: CGEventFlags = []
        var code: CGKeyCode?
        var needShift = false
        for token in text.split(whereSeparator: { $0 == "+" }) {
            let r = Keys.resolve(String(token))
            if let m = r.mod { mods.insert(m) }
            else if let c = r.code { code = c; if r.shift { needShift = true } }
            else { throw KeyError.unknown(String(token)) }
        }
        guard let code else { throw KeyError.unknown(text) }
        if needShift { mods.insert(.maskShift) }
        for _ in 0..<max(1, repeatCount) {
            for down in [true, false] {
                guard let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down) else { continue }
                e.flags = mods
                post(e)
            }
            pause(0.01)
        }
    }

    /// Trzyma chord przez zadany czas: down, czekaj, up.
    static func holdKey(_ text: String, duration: Double) throws {
        var mods: CGEventFlags = []
        var code: CGKeyCode?
        var needShift = false
        for token in text.split(whereSeparator: { $0 == "+" }) {
            let r = Keys.resolve(String(token))
            if let m = r.mod { mods.insert(m) }
            else if let c = r.code { code = c; if r.shift { needShift = true } }
            else { throw KeyError.unknown(String(token)) }
        }
        if needShift { mods.insert(.maskShift) }
        // sam modyfikator (np. hold_key "shift"): wciskamy klawisz modyfikatora
        let key = code ?? modifierKeyCode(mods)
        guard let key else { throw KeyError.unknown(text) }
        if let e = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: true) { e.flags = mods; post(e) }
        pause(min(max(0, duration), 300))
        if let e = CGEvent(keyboardEventSource: nil, virtualKey: key, keyDown: false) { e.flags = []; post(e) }
    }

    private static func modifierKeyCode(_ mods: CGEventFlags) -> CGKeyCode? {
        if mods.contains(.maskShift) { return 0x38 }
        if mods.contains(.maskControl) { return 0x3B }
        if mods.contains(.maskAlternate) { return 0x3A }
        if mods.contains(.maskCommand) { return 0x37 }
        return nil
    }

    static func cursorPosition() -> CGPoint {
        CGEvent(source: nil)?.location ?? .zero
    }
}
