// The guard's sentences and numbers as devguard_core and janitor write them: the reasons in plans,
// the labels in the snapshot and the log lines must be the same text the Python guard writes.
import Darwin

public enum GuardText {
    public static let MB = 1_048_576
    public static let GB = 1_073_741_824

    /// Python's f"{x:.Nf}" (correctly rounded, half to even on the binary value, like C's printf).
    public static func fixed(_ x: Double, _ digits: Int) -> String {
        var buffer = [CChar](repeating: 0, count: 64)
        _ = withVaList([x]) { vsnprintf(&buffer, buffer.count, "%.\(digits)f", $0) }
        return cText(buffer)
    }

    /// janitor.human: "31,6 GB", "412 MB".
    public static func human(_ size: Double) -> String {
        var size = size
        for unit in ["B", "KB", "MB", "GB", "TB"] {
            if size < 1024 || unit == "TB" {
                let text = unit == "GB" || unit == "TB" ? fixed(size, 1) : fixed(size, 0)
                return text.replacingDot() + " " + unit
            }
            size /= 1024
        }
        return ""
    }

    public static func human(_ size: Int) -> String { human(Double(size)) }

    /// devguard_core.short: the home as ~, and /Documents/ dropped.
    public static func short(_ path: String, home: String) -> String {
        var p = path
        if let range = p.pyRange(of: home) { p.replaceSubrange(range, with: "~") }
        return p.pyReplace("/Documents/", "/")
    }

    /// devguard_core.minutes
    public static func minutes(_ seconds: Double) -> String { "\(int((seconds / 60).rounded(.down))) min" }

    /// Python round(x) for a float: half to even, an int.
    public static func round(_ x: Double) -> Int { int(x.rounded(.toNearestOrEven)) }

    /// int(x) for a float without trapping: NaN and the infinities (where Python raises and the tick
    /// fails) read as 0, values beyond Int64 stop at its ends. A daemon must not die of a state value.
    public static func int(_ x: Double) -> Int {
        guard x.isFinite else { return 0 }
        if x >= 9.223372036854775807e18 { return .max }
        if x <= -9.223372036854775808e18 { return .min }
        return Int(x)
    }

    /// Python round(x, n) for a float, as close as a double gets (Python rounds the exact decimal).
    public static func round(_ x: Double, _ n: Int) -> Double {
        // Python's round(x, n) uses the correctly rounded decimal string of x
        let text = fixed(x, n)
        return Double(text) ?? x
    }

    /// shlex.quote
    public static func quote(_ s: String) -> String {
        if s.isEmpty { return "''" }
        let safe = s.unicodeScalars.allSatisfy { c in
            c.isASCII && (("a"..."z").contains(c) || ("A"..."Z").contains(c) || ("0"..."9").contains(c) || "_@%+=:,./-".unicodeScalars.contains(c))
        }
        if safe { return s }
        return "'" + s.pyReplace("'", "'\"'\"'") + "'"
    }

    /// shlex.join
    public static func join(_ argv: [String]) -> String { argv.map(quote).joined(separator: " ") }

    /// command[:n] in Python: code points, not characters.
    public static func prefix(_ s: String, _ n: Int) -> String {
        var out = String.UnicodeScalarView()
        for (i, c) in s.unicodeScalars.enumerated() {
            if i == n { break }
            out.append(c)
        }
        return String(out)
    }
}

extension String {
    func replacingDot() -> String { pyReplace(".", ",") }
}
