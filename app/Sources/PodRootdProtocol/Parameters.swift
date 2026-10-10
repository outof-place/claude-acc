/// An integer parameter that only holds values from its range. Decoding anything else fails, so a
/// verb that reaches the helper's engine is valid by construction; `isValid` checks a value built
/// in code with `init(unchecked:)`.
public protocol BoundedValue: Codable, Hashable, Sendable, CustomStringConvertible {
    static var allowed: ClosedRange<Int64> { get }
    var value: Int64 { get }
    init(unchecked value: Int64)
}

extension BoundedValue {
    public init?(_ value: Int64) {
        guard Self.allowed.contains(value) else { return nil }
        self.init(unchecked: value)
    }

    public init?(_ value: Int) { self.init(Int64(value)) }

    public var isValid: Bool { Self.allowed.contains(value) }

    public init(from decoder: any Decoder) throws {
        let value = try decoder.singleValueContainer().decode(Int64.self)
        guard let valid = Self(value) else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "\(value) is outside \(Self.allowed)"))
        }
        self = valid
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(value)
    }

    public var description: String { String(value) }
}

/// A fixed fan setting, as a share of each fan's min-max range (`fanctl set` takes the same).
public struct FanPercent: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 30...100
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
}

/// How long one lid hold lasts: a minute to `Lid.maxHold`, a day.
public struct LidSeconds: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 60...86_400
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
}

/// `kern.maxvnodes`: from the kernel's default on the M4 Max up to ~2.5 GB of kernel memory
/// (about 1.2 KB a vnode, perf-root.sh).
public struct MaxVnodes: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 263_168...2_097_152
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
}

/// `iogpu.wired_limit_mb` other than macOS's default; the helper also keeps 4 GB of the Mac's RAM
/// for the system.
public struct GPUMegabytes: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 4096...1_048_576
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
}

/// An upload limit for `ifconfig <if> tbr`, in kb/s: 1 Mb/s to 10 Gb/s.
public struct UplinkKbps: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 1000...10_000_000
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
}

/// fseventsd's footprint that counts as runaway, in MB (fsguard.py: 4096).
public struct FSGuardLimitMB: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 1024...65_536
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
    public static let standard = FSGuardLimitMB(unchecked: 4096)
}

/// Diagnostic reports older than this many days go (janitor-root.sh: 30).
public struct DiagnosticAgeDays: BoundedValue {
    public static let allowed: ClosedRange<Int64> = 30...365
    public let value: Int64
    public init(unchecked value: Int64) { self.value = value }
    public static let standard = DiagnosticAgeDays(unchecked: 30)
}

/// A network interface the upload limit may go on: `en0` to `en999` (Wi-Fi, Ethernet, iPhone USB,
/// Bluetooth PAN). The helper also checks it exists.
public struct InterfaceName: Codable, Hashable, Sendable, CustomStringConvertible {
    public let name: String

    public init?(_ name: String) {
        let digits = name.utf8.dropFirst(2)
        guard name.hasPrefix("en"), (1...3).contains(digits.count), digits.allSatisfy({ (48...57).contains($0) })
        else { return nil }
        self.name = name
    }

    public init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        guard let valid = InterfaceName(raw) else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath, debugDescription: "\(raw.debugDescription) is not en<N>"))
        }
        self = valid
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(name)
    }

    public var description: String { name }
}

public enum FanMode: Codable, Hashable, Sendable, CustomStringConvertible {
    /// macOS decides.
    case auto
    case fixed(FanPercent)

    public var description: String {
        switch self {
        case .auto: "auto"
        case .fixed(let percent): "\(percent)%"
        }
    }
}

public enum PowerSource: String, Codable, Sendable, CaseIterable {
    case ac
    case battery

    /// pmset's flag for the setting on this source.
    public var pmsetFlag: String { self == .ac ? "-c" : "-b" }
}

/// `pmset powermode`: 0 automatic, 1 low power, 2 high power (M Max on a charger).
public enum PowerMode: Int64, Codable, Sendable, CaseIterable {
    case automatic = 0
    case low = 1
    case high = 2

    public var name: String {
        switch self {
        case .automatic: "automatic"
        case .low: "low"
        case .high: "high"
        }
    }
}

/// How long an upload limit holds.
public enum ShaperScope: String, Codable, Sendable {
    /// Until the session that set it ends (the hotspot controller).
    case session
    /// Until the Mac restarts or someone clears it (perf-root's `shaper apply`).
    case untilReboot
}

public enum SysctlKey: String, Codable, Sendable, CaseIterable {
    case maxVnodes = "kern.maxvnodes"
    case gpuWiredLimitMB = "iogpu.wired_limit_mb"
}

/// macOS's own GPU wired limit (about two thirds of RAM), or a number of MB.
public enum GPULimit: Codable, Hashable, Sendable {
    case systemDefault
    case megabytes(GPUMegabytes)

    public var value: Int64 {
        switch self {
        case .systemDefault: 0
        case .megabytes(let mb): mb.value
        }
    }
}

public enum SysctlSetting: Codable, Hashable, Sendable {
    case maxVnodes(MaxVnodes)
    case gpuWiredLimit(GPULimit)

    public var key: SysctlKey {
        switch self {
        case .maxVnodes: .maxVnodes
        case .gpuWiredLimit: .gpuWiredLimitMB
        }
    }

    public var value: Int64 {
        switch self {
        case .maxVnodes(let n): n.value
        case .gpuWiredLimit(let limit): limit.value
        }
    }
}
