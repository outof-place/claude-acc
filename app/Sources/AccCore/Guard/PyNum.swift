// Python's number arithmetic where it shows: sum() of floats is compensated since 3.12 (Neumaier,
// CPython's builtin_sum), an empty sum is the int 0, and an int stays an int in the state file.
// Where Python's int would grow past Int64 the result is a float instead of a trap.

/// A number as Python holds it: int or float.
public enum PyNum: Equatable, Sendable, Comparable {
    case int(Int)
    case double(Double)

    public var value: Double {
        switch self {
        case .int(let i): Double(i)
        case .double(let d): d
        }
    }

    public var json: PyJSON {
        switch self {
        case .int(let i): .int(i)
        case .double(let d): .double(d)
        }
    }

    public init?(_ json: PyJSON?) {
        switch json {
        case .int(let i)?: self = .int(i)
        case .double(let d)?: self = .double(d)
        case .bool(let b)?: self = .int(b ? 1 : 0)
        default: return nil
        }
    }

    /// `x or default`: None, 0 and 0.0 give the default
    public static func or(_ json: PyJSON?, _ fallback: PyNum) -> PyNum {
        guard let n = PyNum(json), n.value != 0 else { return fallback }
        return n
    }

    public var truthy: Bool { value != 0 }

    public static func < (a: PyNum, b: PyNum) -> Bool { a.value < b.value }
    public static func == (a: PyNum, b: PyNum) -> Bool {
        switch (a, b) {
        case (.int(let x), .int(let y)): x == y
        default: a.value == b.value
        }
    }

    /// a - b with Python's types
    public static func - (a: PyNum, b: PyNum) -> PyNum {
        if case .int(let x) = a, case .int(let y) = b, case let (r, false) = x.subtractingReportingOverflow(y) { return .int(r) }
        return .double(a.value - b.value)
    }

    public static func + (a: PyNum, b: PyNum) -> PyNum {
        if case .int(let x) = a, case .int(let y) = b, case let (r, false) = x.addingReportingOverflow(y) { return .int(r) }
        return .double(a.value + b.value)
    }

    public static func * (a: PyNum, b: PyNum) -> PyNum {
        if case .int(let x) = a, case .int(let y) = b, case let (r, false) = x.multipliedReportingOverflow(by: y) { return .int(r) }
        return .double(a.value * b.value)
    }

    /// true division: always a float
    public static func / (a: PyNum, b: PyNum) -> PyNum { .double(a.value / b.value) }
}

/// max(a, b): the first of the largest, with its own type
public func pyMax(_ a: PyNum, _ b: PyNum) -> PyNum { b > a ? b : a }
/// min(a, b): the first of the smallest
public func pyMin(_ a: PyNum, _ b: PyNum) -> PyNum { b < a ? b : a }

/// round(x, n): an int stays an int; a float rounds to the decimal Python's round gives
public func pyRound(_ x: PyNum, _ n: Int) -> PyNum {
    switch x {
    case .int: x
    case .double(let d): .double(d.isFinite ? GuardText.round(d, n) : d)
    }
}

/// builtin sum() over floats (CPython 3.12+): the first float joins the int start by plain addition,
/// the rest go through the compensated sum; no items at all is the int 0.
public func pySum(_ xs: [Double]) -> PyNum {
    guard let first = xs.first else { return .int(0) }
    var hi = 0.0 + first
    var lo = 0.0
    for x in xs.dropFirst() {
        let t = hi + x
        if Swift.abs(hi) >= Swift.abs(x) {
            lo += (hi - t) + x
        } else {
            lo += (x - t) + hi
        }
        hi = t
    }
    if lo != 0, lo.isFinite { return .double(hi + lo) }
    return .double(hi)
}

/// builtin sum() over ints and floats in any mix: the int fast path until the first float, then the
/// compensated sum (later ints join it as floats)
public func pySum(_ xs: [PyNum]) -> PyNum {
    var i = 0
    var total = 0
    while i < xs.count, case .int(let v) = xs[i], case let (t, false) = total.addingReportingOverflow(v) {
        total = t
        i += 1
    }
    if i == xs.count { return .int(total) }
    var floats = [Double(total) + xs[i].value]
    floats += xs[(i + 1)...].map(\.value)
    return pySum(floats)
}
