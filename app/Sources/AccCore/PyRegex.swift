// Python `re` patterns on ICU (NSRegularExpression) with Python's meaning of the classes they use:
// ICU's \s leaves out \v, \x1c-\x1f and \x85, its \w and \b take in combining marks, its `.` stops at
// U+2028 and its `$` before a final line break. The guard's patterns run over ps-style command lines,
// so a match must be the one devguard_core gets; tests/acc_cored/parity_regex.py checks them on a corpus.
// A construct outside the translated subset stops the build of the pattern (a precondition): the
// patterns are constants, so a test that loads them finds it.
import Darwin
import Foundation

public struct PyRegex: @unchecked Sendable {
    public let pattern: String
    let regex: NSRegularExpression

    /// Python's str \s (str.isspace) and \w ([\p{L}\p{N}_], what str.isalnum() and _ cover).
    static let space = #"[\t\n\x{0B}\f\r\x{1C}-\x{1F}\x{85}\p{Z}]"#
    static let notSpace = #"[^\t\n\x{0B}\f\r\x{1C}-\x{1F}\x{85}\p{Z}]"#
    static let word = #"[\p{L}\p{N}_]"#
    static let notWord = #"[^\p{L}\p{N}_]"#
    static let wordInClass = #"\p{L}\p{N}_"#
    static let spaceInClass = #"\t\n\x{0B}\f\r\x{1C}-\x{1F}\x{85}\p{Z}"#

    /// Literals one of which every match contains: a line without any is a miss without ICU (most
    /// of the table, at a memmem each instead of an NSString bridge and a match).
    let anyOf: [[UInt8]]

    public init(_ pattern: String, anyOf: [String] = []) {
        self.pattern = pattern
        self.anyOf = anyOf.map { Array($0.utf8) }
        guard let translated = Self.translate(pattern) else { preconditionFailure("PyRegex can't translate \(pattern)") }
        regex = try! NSRegularExpression(pattern: translated)
    }

    @inline(__always)
    func mayMatch(_ s: String) -> Bool {
        guard !anyOf.isEmpty else { return true }
        var s = s
        return s.withUTF8 { hay in
            anyOf.contains { needle in
                needle.withUnsafeBytes { n in memmem(hay.baseAddress, hay.count, n.baseAddress, n.count) != nil }
            }
        }
    }

    /// The pattern with Python's meaning spelled out for ICU: \s \S \w \W \b \B \v \Z . and $, and
    /// inside a class the punctuation ICU reads as set syntax (`[`, `&`, `:`, …) escaped. nil for what
    /// isn't translated: inline flags, (?P…), `--` inside a class.
    static func translate(_ p: String) -> String? {
        var out = ""
        let chars = Array(p.unicodeScalars)
        var i = 0
        var inClass = false
        while i < chars.count {
            let c = chars[i]
            if c == "\\", i + 1 < chars.count {
                let n = chars[i + 1]
                i += 2
                switch (n, inClass) {
                case ("s", false): out += space
                case ("S", false): out += notSpace
                case ("w", false): out += word
                case ("W", false): out += notWord
                case ("s", true): out += spaceInClass
                case ("w", true): out += wordInClass
                case ("S", true): out += "[^" + spaceInClass + "]"
                case ("W", true): out += "[^" + wordInClass + "]"
                case ("b", false): out += "(?:(?<=\(word))(?!\(word))|(?<!\(word))(?=\(word)))"
                case ("B", false): out += "(?:(?<=\(word))(?=\(word))|(?<!\(word))(?!\(word)))"
                case ("b", true): out += "\\x{08}"
                case ("v", _): out += "\\x{0B}"
                case ("Z", false): out += "\\z"
                default: out += "\\" + String(n)
                }
                continue
            }
            if inClass {
                if c == "]" {
                    inClass = false
                } else if c == "-" {
                    if i + 1 < chars.count, chars[i + 1] == "-" { return nil }
                } else if c.value > 0x20, c.value < 0x7F, !c.properties.isAlphabetic, !("0"..."9").contains(c) {
                    out += "\\"  // literal in Python, maybe set syntax in ICU
                }
                out.unicodeScalars.append(c)
            } else if c == "[" {
                inClass = true
                out.unicodeScalars.append(c)
                // a ] right after [ or [^ is a literal
                if i + 1 < chars.count, chars[i + 1] == "^" { out.append("^"); i += 1 }
                if i + 1 < chars.count, chars[i + 1] == "]" { out.append("\\]"); i += 1 }
            } else if c == "(", i + 2 < chars.count, chars[i + 1] == "?",
                      !([":", "=", "!", "<", ">", "#"] as [Unicode.Scalar]).contains(chars[i + 2])
                          || (chars[i + 2] == "<" && i + 3 < chars.count && !(["=", "!"] as [Unicode.Scalar]).contains(chars[i + 3]))
            {
                return nil  // inline flags, (?P<name>…), (?(1)…): not translated
            } else if c == "." {
                out += "[^\\n]"
            } else if c == "$" {
                out += "(?=\\n?\\z)"
            } else {
                out.unicodeScalars.append(c)
            }
            i += 1
        }
        return out
    }

    /// re.search: any match at all.
    public func search(_ s: String) -> Bool {
        guard mayMatch(s) else { return false }
        return regex.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    /// re.match: a match starting at the beginning.
    public func match(_ s: String) -> Bool {
        guard mayMatch(s) else { return false }
        return regex.firstMatch(in: s, options: [.anchored], range: NSRange(s.startIndex..., in: s)) != nil
    }

    /// The groups of the first match (re.search(...).groups()), nil when it doesn't match.
    public func groups(_ s: String) -> [String?]? {
        guard mayMatch(s), let m = regex.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return (1..<m.numberOfRanges).map { i in
            Range(m.range(at: i), in: s).map { String(s[$0]) }
        }
    }

    /// Escaped for use inside a pattern, as re.escape.
    public static func escape(_ s: String) -> String {
        var out = ""
        for scalar in s.unicodeScalars {
            let ch = Character(scalar)
            if scalar.isASCII, !(ch.isLetter || ch.isNumber || ch == "_") { out += "\\" }
            out.unicodeScalars.append(scalar)
        }
        return out
    }
}
