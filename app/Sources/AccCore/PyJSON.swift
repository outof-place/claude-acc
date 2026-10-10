// JSON the way claude-acc's Python reads and writes it: objects keep their key order, 1 and 1.0 stay
// an int and a float, and `dumps` gives json.dumps's bytes (", " and ": ", ensure_ascii, float repr).
// acc-cored shares state files with the scripts, so a file it rewrites must read back the same there.

public enum PyJSON: Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([PyJSON])
    case object(PyObject)

    public subscript(key: String) -> PyJSON? {
        get { if case .object(let o) = self { return o[key] } else { return nil } }
        set {
            guard case .object(var o) = self else { return }
            o[key] = newValue
            self = .object(o)
        }
    }

    public var double: Double? {
        switch self {
        case .int(let i): Double(i)
        case .double(let d): d
        case .bool(let b): b ? 1 : 0
        default: nil
        }
    }

    public var int: Int? {
        switch self {
        case .int(let i): i
        case .bool(let b): b ? 1 : 0
        default: nil
        }
    }

    public var string: String? { if case .string(let s) = self { s } else { nil } }
    public var array: [PyJSON]? { if case .array(let a) = self { a } else { nil } }
    public var object: PyObject? { if case .object(let o) = self { o } else { nil } }
    public var isNull: Bool { self == .null }

    /// Python truthiness: None, False, 0, 0.0, "", [] and {} are false.
    public var truthy: Bool {
        switch self {
        case .null: false
        case .bool(let b): b
        case .int(let i): i != 0
        case .double(let d): d != 0
        case .string(let s): !s.isEmpty
        case .array(let a): !a.isEmpty
        case .object(let o): !o.isEmpty
        }
    }
}

/// A dict with insertion order, as Python's.
public struct PyObject: Equatable, Sendable, Sequence {
    public private(set) var keys: [String] = []
    private var values: [String: PyJSON] = [:]

    public init() {}
    public init(_ pairs: [(String, PyJSON)]) {
        for (k, v) in pairs { self[k] = v }
    }

    public var isEmpty: Bool { keys.isEmpty }
    public var count: Int { keys.count }

    public subscript(key: String) -> PyJSON? {
        get { values[key] }
        set {
            if let newValue {
                if values.updateValue(newValue, forKey: key) == nil { keys.append(key) }
            } else if values.removeValue(forKey: key) != nil {
                keys.removeAll { $0 == key }
            }
        }
    }

    public func makeIterator() -> AnyIterator<(key: String, value: PyJSON)> {
        var i = 0
        return AnyIterator {
            guard i < keys.count else { return nil }
            defer { i += 1 }
            return (keys[i], values[keys[i]]!)
        }
    }

    public static func == (a: PyObject, b: PyObject) -> Bool { a.keys == b.keys && a.values == b.values }
}

extension PyJSON: ExpressibleByBooleanLiteral, ExpressibleByIntegerLiteral,
    ExpressibleByFloatLiteral, ExpressibleByStringLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral
{
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .int(value) }
    public init(floatLiteral value: Double) { self = .double(value) }
    public init(stringLiteral value: String) { self = .string(value) }
    public init(arrayLiteral elements: PyJSON...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, PyJSON)...) { self = .object(PyObject(elements)) }
}

// MARK: - dumps

extension PyJSON {
    /// json.dumps(value) with the defaults: ensure_ascii, ", " and ": ", NaN as NaN.
    public func dumps() -> String {
        var out = ""
        out.reserveCapacity(4096)
        write(to: &out)
        return out
    }

    func write(to out: inout String) {
        switch self {
        case .null: out += "null"
        case .bool(let b): out += b ? "true" : "false"
        case .int(let i): out += String(i)
        case .double(let d): out += Self.repr(d)
        case .string(let s): Self.quote(s, into: &out)
        case .array(let a):
            out += "["
            for (i, v) in a.enumerated() {
                if i > 0 { out += ", " }
                v.write(to: &out)
            }
            out += "]"
        case .object(let o):
            out += "{"
            var first = true
            for (k, v) in o {
                if !first { out += ", " }
                first = false
                Self.quote(k, into: &out)
                out += ": "
                v.write(to: &out)
            }
            out += "}"
        }
    }

    static func quote(_ s: String, into out: inout String) {
        out += "\""
        for unit in s.utf16 {
            switch unit {
            case 0x22: out += "\\\""
            case 0x5C: out += "\\\\"
            case 0x0A: out += "\\n"
            case 0x0D: out += "\\r"
            case 0x09: out += "\\t"
            case 0x08: out += "\\b"
            case 0x0C: out += "\\f"
            case 0x20..<0x7F: out.unicodeScalars.append(Unicode.Scalar(UInt8(unit)))
            default:
                let hex = String(unit, radix: 16)
                out += "\\u" + String(repeating: "0", count: 4 - hex.count) + hex
            }
        }
        out += "\""
    }

    /// float.__repr__: the shortest digits that read back the same, fixed notation for decimal
    /// exponents -4 < e <= 16, else d.ddde±XX.
    public static func repr(_ d: Double) -> String {
        if d.isNaN { return "NaN" }
        if d.isInfinite { return d < 0 ? "-Infinity" : "Infinity" }
        if d == 0 { return d.sign == .minus ? "-0.0" : "0.0" }
        // Swift's description already holds the shortest round-trip digits; take them apart
        var text = Swift.abs(d).description
        var exponent = 0
        if let e = text.firstIndex(where: { $0 == "e" || $0 == "E" }) {
            exponent = Int(text[text.index(after: e)...])!
            text = String(text[..<e])
        }
        var digits = text.replacingOccurrences(of: ".", with: "")
        let point = text.firstIndex(of: ".").map { text.distance(from: text.startIndex, to: $0) } ?? text.count
        var decpt = point + exponent  // value = 0.digits × 10^decpt
        while digits.hasPrefix("0"), digits.count > 1 {
            digits.removeFirst()
            decpt -= 1
        }
        while digits.hasSuffix("0"), digits.count > 1 { digits.removeLast() }
        let sign = d < 0 ? "-" : ""
        if decpt <= -4 || decpt > 16 {
            let mantissa = digits.count > 1 ? String(digits.first!) + "." + digits.dropFirst() : digits
            let e = decpt - 1
            let ed = String(Swift.abs(e))
            return sign + mantissa + "e" + (e < 0 ? "-" : "+") + (ed.count < 2 ? "0" + ed : ed)
        }
        if decpt <= 0 { return sign + "0." + String(repeating: "0", count: -decpt) + digits }
        if decpt >= digits.count { return sign + digits + String(repeating: "0", count: decpt - digits.count) + ".0" }
        let i = digits.index(digits.startIndex, offsetBy: decpt)
        return sign + digits[..<i] + "." + digits[i...]
    }
}

// MARK: - loads

extension PyJSON {
    public struct ParseError: Error, Equatable { public let offset: Int }

    /// json.loads: ints stay ints, anything with a fraction or exponent is a float.
    public static func loads(_ text: String) throws -> PyJSON {
        var parser = Parser(bytes: Array(text.utf8))
        let value = try parser.value()
        parser.space()
        guard parser.i == parser.bytes.count else { throw ParseError(offset: parser.i) }
        return value
    }

    public static func loads(bytes: [UInt8]) throws -> PyJSON {
        var parser = Parser(bytes: bytes)
        let value = try parser.value()
        parser.space()
        guard parser.i == parser.bytes.count else { throw ParseError(offset: parser.i) }
        return value
    }

    struct Parser {
        let bytes: [UInt8]
        var i = 0

        mutating func space() {
            while i < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[i]) { i += 1 }
        }

        mutating func value() throws -> PyJSON {
            space()
            guard i < bytes.count else { throw ParseError(offset: i) }
            switch bytes[i] {
            case UInt8(ascii: "{"):
                i += 1
                var o = PyObject()
                space()
                if i < bytes.count, bytes[i] == UInt8(ascii: "}") { i += 1; return .object(o) }
                while true {
                    space()
                    guard i < bytes.count, bytes[i] == UInt8(ascii: "\"") else { throw ParseError(offset: i) }
                    let k = try string()
                    space()
                    guard i < bytes.count, bytes[i] == UInt8(ascii: ":") else { throw ParseError(offset: i) }
                    i += 1
                    o[k] = try value()  // a repeated key keeps its first position, the last value (as dict)
                    space()
                    guard i < bytes.count else { throw ParseError(offset: i) }
                    if bytes[i] == UInt8(ascii: ",") { i += 1; continue }
                    if bytes[i] == UInt8(ascii: "}") { i += 1; return .object(o) }
                    throw ParseError(offset: i)
                }
            case UInt8(ascii: "["):
                i += 1
                var a: [PyJSON] = []
                space()
                if i < bytes.count, bytes[i] == UInt8(ascii: "]") { i += 1; return .array(a) }
                while true {
                    a.append(try value())
                    space()
                    guard i < bytes.count else { throw ParseError(offset: i) }
                    if bytes[i] == UInt8(ascii: ",") { i += 1; continue }
                    if bytes[i] == UInt8(ascii: "]") { i += 1; return .array(a) }
                    throw ParseError(offset: i)
                }
            case UInt8(ascii: "\""): return .string(try string())
            case UInt8(ascii: "t"): try literal("true"); return .bool(true)
            case UInt8(ascii: "f"): try literal("false"); return .bool(false)
            case UInt8(ascii: "n"): try literal("null"); return .null
            case UInt8(ascii: "N"): try literal("NaN"); return .double(.nan)
            case UInt8(ascii: "I"): try literal("Infinity"); return .double(.infinity)
            default: return try number()
            }
        }

        mutating func literal(_ word: String) throws {
            let w = Array(word.utf8)
            guard i + w.count <= bytes.count, Array(bytes[i..<i + w.count]) == w else { throw ParseError(offset: i) }
            i += w.count
        }

        mutating func number() throws -> PyJSON {
            let start = i
            if i < bytes.count, bytes[i] == UInt8(ascii: "-") {
                i += 1
                if i < bytes.count, bytes[i] == UInt8(ascii: "I") { try literal("Infinity"); return .double(-.infinity) }
            }
            var float = false
            while i < bytes.count {
                let c = bytes[i]
                if c >= 0x30 && c <= 0x39 || c == UInt8(ascii: "-") || c == UInt8(ascii: "+") {
                    i += 1
                } else if c == UInt8(ascii: ".") || c == UInt8(ascii: "e") || c == UInt8(ascii: "E") {
                    float = true
                    i += 1
                } else {
                    break
                }
            }
            let text = String(decoding: bytes[start..<i], as: UTF8.self)
            if !float, let n = Int(text) { return .int(n) }
            guard let d = Double(text) else { throw ParseError(offset: start) }
            return .double(d)
        }

        mutating func string() throws -> String {
            i += 1  // the opening quote
            var units: [UInt16] = []
            var run = i
            var out = ""
            func flush(_ end: Int) { out += String(decoding: bytes[run..<end], as: UTF8.self) }
            while i < bytes.count {
                let c = bytes[i]
                if c == UInt8(ascii: "\"") {
                    flush(i)
                    i += 1
                    return out
                }
                if c == UInt8(ascii: "\\") {
                    flush(i)
                    i += 1
                    guard i < bytes.count else { break }
                    let e = bytes[i]
                    i += 1
                    switch e {
                    case UInt8(ascii: "n"): out += "\n"
                    case UInt8(ascii: "t"): out += "\t"
                    case UInt8(ascii: "r"): out += "\r"
                    case UInt8(ascii: "b"): out += "\u{8}"
                    case UInt8(ascii: "f"): out += "\u{c}"
                    case UInt8(ascii: "u"):
                        guard i + 4 <= bytes.count, let u = UInt16(String(decoding: bytes[i..<i + 4], as: UTF8.self), radix: 16) else {
                            throw ParseError(offset: i)
                        }
                        i += 4
                        units = [u]
                        // a surrogate pair is two escapes in a row
                        if u >= 0xD800, u < 0xDC00, i + 6 <= bytes.count, bytes[i] == UInt8(ascii: "\\"), bytes[i + 1] == UInt8(ascii: "u"),
                           let low = UInt16(String(decoding: bytes[i + 2..<i + 6], as: UTF8.self), radix: 16), low >= 0xDC00, low < 0xE000
                        {
                            units.append(low)
                            i += 6
                        }
                        out += String(decoding: units, as: UTF16.self)
                    default: out.unicodeScalars.append(Unicode.Scalar(e))
                    }
                    run = i
                    continue
                }
                i += 1
            }
            throw ParseError(offset: i)
        }
    }
}
