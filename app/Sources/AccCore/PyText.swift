// str and os.path as Python has them, by code point (Swift's String works by grapheme): the guard's
// lines and paths must split, compare and sort the way devguard_core's do.

extension String {
    /// str.replace for plain substrings (no Foundation).
    public func pyReplace(_ target: String, _ replacement: String) -> String {
        guard !target.isEmpty else { return self }
        var out = ""
        var rest = self[...]
        while let r = rest.pyRange(of: target) {
            out += rest[..<r.lowerBound]
            out += replacement
            rest = rest[r.upperBound...]
        }
        out += rest
        return out
    }
}

extension Substring {
    /// The first occurrence of `target`, by unicode scalars (str.find semantics on code points).
    func pyRange(of target: String) -> Range<Substring.Index>? {
        let t = Array(target.unicodeScalars)
        guard !t.isEmpty else { return startIndex..<startIndex }
        let scalars = unicodeScalars
        var i = scalars.startIndex
        while i != scalars.endIndex {
            var j = i
            var k = 0
            while k < t.count, j != scalars.endIndex, scalars[j] == t[k] {
                j = scalars.index(after: j)
                k += 1
            }
            if k == t.count { return i..<j }
            i = scalars.index(after: i)
        }
        return nil
    }
}

extension String {
    func pyRange(of target: String) -> Range<String.Index>? {
        self[...].pyRange(of: target)
    }

    /// `target in self` (str.__contains__, by code points)
    public func pyContains(_ target: String) -> Bool { pyRange(of: target) != nil }

    /// str.startswith, by code points
    public func pyStarts(_ prefix: String) -> Bool { unicodeScalars.starts(with: prefix.unicodeScalars) }

    /// str.endswith, by code points
    public func pyEnds(_ suffix: String) -> Bool { unicodeScalars.reversed().starts(with: suffix.unicodeScalars.reversed()) }
}

/// str.split(None, 1)[0] (or "" for an empty or blank command)
func firstWord(_ s: String) -> String {
    var out = String.UnicodeScalarView()
    var started = false
    for c in s.unicodeScalars {
        if pyIsSpace(c) {
            if started { break }
            continue
        }
        started = true
        out.append(c)
    }
    return String(out)
}

/// str.isspace for one code point
func pyIsSpace(_ c: Unicode.Scalar) -> Bool {
    switch c.value {
    case 0x09...0x0D, 0x1C...0x20, 0x85, 0xA0, 0x1680, 0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000: true
    default: false
    }
}

extension String {
    func trimmingPySpace() -> String {
        let s = unicodeScalars
        guard let a = s.firstIndex(where: { !pyIsSpace($0) }), let b = s.lastIndex(where: { !pyIsSpace($0) }) else { return "" }
        return String(s[a...b])
    }
}

/// sorted() of strings: by code point, as Python compares str
public func pySorted<S: Sequence<String>>(_ items: S) -> [String] {
    items.sorted(by: pyLess)
}

public func pyLess(_ a: String, _ b: String) -> Bool {
    a.unicodeScalars.lexicographicallyPrecedes(b.unicodeScalars)
}

/// os.path.dirname, split at the last "/" code point (a "/" may carry a combining mark, which a
/// Character search would miss)
public func dirname(_ s: String) -> String {
    let u = s.utf8
    guard let slash = u.lastIndex(of: 0x2F) else { return "" }
    var head = u[...slash]
    // the head loses its trailing slashes unless it is nothing else
    if !head.allSatisfy({ $0 == 0x2F }) {
        while head.last == 0x2F { head = head.dropLast() }
    }
    return String(Substring(head))
}

/// os.path.basename
public func basename(_ s: String) -> String {
    let u = s.utf8
    guard let slash = u.lastIndex(of: 0x2F) else { return s }
    return String(Substring(u[u.index(after: slash)...]))
}

extension String {
    /// str.partition
    func pyPartition(_ sep: String) -> (String, Bool, String) {
        guard let r = pyRange(of: sep) else { return (self, false, "") }
        return (String(self[..<r.lowerBound]), true, String(self[r.upperBound...]))
    }

    /// str.rpartition
    func pyRPartition(_ sep: String) -> (String, Bool, String) {
        var last: Range<String.Index>?
        var from = startIndex
        while let r = self[from...].pyRange(of: sep) {
            last = r
            from = r.upperBound
            if r.isEmpty { break }
        }
        guard let last else { return ("", false, self) }
        return (String(self[..<last.lowerBound]), true, String(self[last.upperBound...]))
    }
}

/// str.isdigit for one code point (decimal digits and the superscripts Python also counts)
func pyIsDigit(_ c: Unicode.Scalar) -> Bool {
    c.properties.numericType == .decimal || c.properties.numericType == .digit
}
