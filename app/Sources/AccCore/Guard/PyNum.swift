// Python's number arithmetic where it shows: sum() of floats is compensated since 3.12 (Neumaier,
// CPython's builtin_sum), an empty sum is the int 0, and an int stays an int in the state file.

/// A number as Python holds it: int or float.
public enum PyNum: Equatable, Sendable {
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

    /// a - b with Python's types
    public static func - (a: PyNum, b: PyNum) -> PyNum {
        if case .int(let x) = a, case .int(let y) = b { return .int(x - y) }
        return .double(a.value - b.value)
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
