import CoreGraphics
import Foundation

// Mapowanie nazw klawiszy (składnia xdotool/X11, jaką model wysyła) na wirtualne kody klawiszy macOS
// i modyfikatory. Litery i cyfry idą z układu ANSI; chord "ctrl+s" to Control+S, a nie Cmd+S, bo tak
// nazywa je toolset. Na macOS skróty to zwykle Cmd: skill mówi agentowi, żeby pisał "cmd+..." tam, gdzie
// trzeba. Tu tłumaczymy wiernie, żeby i Control, i Command działały zgodnie z tym, co model napisał.
enum Keys {
    // nazwane klawisze -> wirtualny kod (CGKeyCode)
    static let named: [String: CGKeyCode] = [
        "return": 0x24, "enter": 0x24, "kp_enter": 0x4C, "tab": 0x30, "space": 0x31, "delete": 0x33,
        "backspace": 0x33, "escape": 0x35, "esc": 0x35, "forwarddelete": 0x75, "del": 0x75,
        "home": 0x73, "end": 0x77, "pageup": 0x74, "page_up": 0x74, "prior": 0x74,
        "pagedown": 0x79, "page_down": 0x79, "next": 0x79,
        "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
        "arrowleft": 0x7B, "arrowright": 0x7C, "arrowdown": 0x7D, "arrowup": 0x7E,
        "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62,
        "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
        "minus": 0x1B, "equal": 0x18, "bracketleft": 0x21, "bracketright": 0x1E, "backslash": 0x2A,
        "semicolon": 0x29, "apostrophe": 0x27, "quoteright": 0x27, "grave": 0x32, "comma": 0x2B,
        "period": 0x2F, "slash": 0x2C, "capslock": 0x39,
    ]
    // modyfikatory: nazwa -> flaga CGEventFlags
    static let modifiers: [String: CGEventFlags] = [
        "shift": .maskShift, "ctrl": .maskControl, "control": .maskControl,
        "alt": .maskAlternate, "option": .maskAlternate, "opt": .maskAlternate,
        "cmd": .maskCommand, "command": .maskCommand, "meta": .maskCommand, "super": .maskCommand, "win": .maskCommand,
        "fn": .maskSecondaryFn,
    ]
    // znaki ANSI -> kod klawisza (bez Shift; wielkie litery i symbole z Shift robi caller przez flagę)
    static let ansi: [Character: (code: CGKeyCode, shift: Bool)] = {
        var m: [Character: (CGKeyCode, Bool)] = [:]
        let rows: [(String, [CGKeyCode])] = [
            ("abcdefghijklmnopqrstuvwxyz",
             [0x00,0x0B,0x08,0x02,0x0E,0x03,0x05,0x04,0x22,0x26,0x28,0x25,0x2E,0x2D,0x1F,0x23,0x0C,0x0F,0x01,0x11,0x20,0x09,0x0D,0x07,0x10,0x06]),
            ("0123456789", [0x1D,0x12,0x13,0x14,0x15,0x17,0x16,0x1A,0x1C,0x19]),
        ]
        for (chars, codes) in rows {
            for (ch, code) in zip(chars, codes) { m[ch] = (code, false) }
        }
        // wielkie litery: ten sam kod + Shift
        for ch in "abcdefghijklmnopqrstuvwxyz" {
            if let (code, _) = m[ch] { m[Character(ch.uppercased())] = (code, true) }
        }
        let punct: [(Character, CGKeyCode, Bool)] = [
            (" ", 0x31, false), ("-", 0x1B, false), ("_", 0x1B, true), ("=", 0x18, false), ("+", 0x18, true),
            ("[", 0x21, false), ("{", 0x21, true), ("]", 0x1E, false), ("}", 0x1E, true),
            ("\\", 0x2A, false), ("|", 0x2A, true), (";", 0x29, false), (":", 0x29, true),
            ("'", 0x27, false), ("\"", 0x27, true), (",", 0x2B, false), ("<", 0x2B, true),
            (".", 0x2F, false), (">", 0x2F, true), ("/", 0x2C, false), ("?", 0x2C, true),
            ("`", 0x32, false), ("~", 0x32, true),
            ("1", 0x12, false), ("!", 0x12, true), ("2", 0x13, false), ("@", 0x13, true),
            ("3", 0x14, false), ("#", 0x14, true), ("4", 0x15, false), ("$", 0x15, true),
            ("5", 0x17, false), ("%", 0x17, true), ("6", 0x16, false), ("^", 0x16, true),
            ("7", 0x1A, false), ("&", 0x1A, true), ("8", 0x1C, false), ("*", 0x1C, true),
            ("9", 0x19, false), ("(", 0x19, true), ("0", 0x1D, false), (")", 0x1D, true),
        ]
        for (ch, code, shift) in punct { m[ch] = (code, shift) }
        return m
    }()

    /// Jeden token chordu ("ctrl", "shift", "a", "Return") na (modyfikator) albo (kod, czy-z-shiftem).
    /// Zwraca nil dla nieznanej nazwy.
    static func resolve(_ token: String) -> (mod: CGEventFlags?, code: CGKeyCode?, shift: Bool) {
        let t = token.trimmingCharacters(in: .whitespaces)
        let low = t.lowercased()
        if let mod = modifiers[low] { return (mod, nil, false) }
        if let code = named[low] { return (nil, code, false) }
        if t.count == 1, let (code, shift) = ansi[t.first!] { return (nil, code, shift) }
        return (nil, nil, false)
    }
}
