import Foundation
import PodRootdProtocol

/// What the helper keeps across restarts (docs/pod-rootd.md, "Restart safety"): what was asked and
/// what was there before. Leases are not here: they end with the helper.
public struct HelperState: Codable, Equatable, Sendable {
    public var version = 1
    public var fans: FanMode = .auto
    /// `SleepDisabled` is on because the helper set it.
    public var lidHeldByUs = false
    /// By `SysctlKey.rawValue`.
    public var sysctls: [String: SysctlRecord] = [:]
    /// By interface name.
    public var shapers: [String: ShaperRecord] = [:]
    public var spotlightApplied = false
    public var spotlightSaved: [String]?
    /// `legacy.migrate` took perf-root.sh's saved list and put its file aside.
    public var spotlightFromLegacy: Bool?
    /// By `PowerSource.rawValue`: the mode before the first change.
    public var powerOriginal: [String: PowerMode] = [:]
    public var fsguard = FSGuardRecord()
    /// Old daemons whose plists the helper moved aside.
    public var legacy: [LegacyDaemon] = []

    public init() {}
}

public struct SysctlRecord: Codable, Equatable, Sendable {
    /// The value before the helper's first change, refreshed from the kernel default at each boot.
    public var original: Int64
    public var value: Int64
    public var persist: Bool
    /// `kern.boottime` when `value` went in.
    public var boot: Int64
}

public struct ShaperRecord: Codable, Equatable, Sendable {
    public var kbps: Int64
    public var previous: Int64?
    public var scope: ShaperScope
    public var boot: Int64
}

public struct FSGuardRecord: Codable, Equatable, Sendable {
    public var enabled = false
    public var limitMB = FSGuardLimitMB.standard.value
    public var overSince: Double?
    public var lastRestart: Double?
    public var restarts = 0
    public var generation = 0
    public var lastTrend: Double?

    public init() {}
}

public protocol StateStore: AnyObject {
    func load() -> HelperState?
    func save(_ state: HelperState) throws
}

/// For tests.
public final class MemoryStateStore: StateStore {
    public var saved: HelperState?
    public private(set) var saves = 0

    public init(_ state: HelperState? = nil) { saved = state }

    public func load() -> HelperState? { saved }

    public func save(_ state: HelperState) throws {
        saved = state
        saves += 1
    }
}

/// JSON in a root-only directory, written atomically: a temporary file opened with O_EXCL and
/// O_NOFOLLOW, fsync, rename. The directory is made 0700 if it is missing.
public final class FileStateStore: StateStore {
    public let path: String

    public init(path: String) { self.path = path }

    public func load() -> HelperState? {
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        guard let data = try? handle.read(upToCount: 1 << 20) else { return nil }
        return try? JSONDecoder().decode(HelperState.self, from: data)
    }

    public func save(_ state: HelperState) throws {
        let dir = (path as NSString).deletingLastPathComponent
        if mkdir(dir, 0o700) != 0, errno != EEXIST {
            throw BackendError("state: mkdir \(dir): \(String(cString: strerror(errno)))")
        }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(state)
        let tmp = path + ".tmp"
        unlink(tmp)
        let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw BackendError("state: open \(tmp): \(String(cString: strerror(errno)))") }
        let written = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        let synced = fsync(fd)
        close(fd)
        guard written == data.count, synced == 0, rename(tmp, path) == 0 else {
            unlink(tmp)
            throw BackendError("state: write \(path): \(String(cString: strerror(errno)))")
        }
    }
}
