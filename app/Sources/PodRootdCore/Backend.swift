import PodRootdProtocol

/// What the engine asks of the machine. `SystemBackend` (pod-rootd) does it with syscalls, IOKit
/// and four Apple tools; `FakeBackend` keeps it in memory for tests. Nothing here takes a free path
/// or a command: every argument is one of the protocol's validated types or a fixed name.
public protocol Backend: AnyObject {
    // MARK: Fans

    /// The fans, and the hottest CPU/GPU sensor when `sensors` (~170 SMC calls on an M4 Max).
    func fanSnapshot(sensors: Bool) throws -> FanSnapshot
    func applyFans(_ mode: FanMode) throws

    // MARK: Sysctls and the machine

    func sysctl(_ key: SysctlKey) throws -> Int64
    func setSysctl(_ key: SysctlKey, to value: Int64) throws
    var memoryMB: Int64 { get }
    /// `kern.boottime` in seconds: a new value means the kernel forgot every sysctl.
    var bootTime: Int64 { get }

    // MARK: Power

    /// `SleepDisabled` in the root power domain, whoever set it.
    func sleepDisabled() -> Bool
    func setSleepDisabled(_ on: Bool) throws
    func powerMode(_ source: PowerSource) -> PowerMode?
    func setPowerMode(_ mode: PowerMode, on source: PowerSource) throws
    var highPowerCapable: Bool { get }
    /// On battery with 10% or less left.
    func batteryLow() -> Bool
    /// macOS's thermal state is serious or worse.
    func thermalSerious() -> Bool

    // MARK: Upload limit

    func interfaceExists(_ interface: InterfaceName) -> Bool
    /// The interface's tbr limit in kb/s, nil without one.
    func uplinkLimit(_ interface: InterfaceName) -> Int64?
    /// nil takes the limit off.
    func setUplinkLimit(_ interface: InterfaceName, kbps: Int64?) throws

    // MARK: Spotlight

    func spotlightExclusions() throws -> [String]
    func setSpotlightExclusions(_ list: [String]) throws
    /// The Privacy list that leaves only applications indexed (perf-root.sh's apps-only), built from
    /// the console user's home and /Applications.
    func appsOnlyExclusions() throws -> [String]
    /// Restarts mds with the list on disk and rebuilds the index.
    func reloadSpotlight()

    // MARK: fseventsd

    func footprint(ofProcessNamed name: String) -> ProcessFootprint?
    /// SIGTERM, then SIGKILL after `grace` seconds if that pid still has that name.
    func terminate(pid: Int32, named name: String, grace: Double)
    func processes(over bytes: UInt64) -> [ProcessFootprint]

    // MARK: launchd and logs

    func launchdOrphans() -> [Orphan]
    func park(_ orphan: Orphan) throws
    func pruneDiagnostics(olderThanDays days: Int64, dryRun: Bool) -> (files: Int, bytes: Int64)

    // MARK: The five old root daemons

    /// The `ProgramArguments` of its plist in /Library/LaunchDaemons; nil when it isn't there.
    func legacyArguments(_ daemon: LegacyDaemon) -> [String]?
    /// The mode in the fans.json the old fans daemon followed.
    func legacyFanMode(configPath: String) -> FanMode?
    /// The Spotlight list perf-root.sh saved before apps-only.
    func legacySpotlightList() -> [String]?
    func bootout(_ daemon: LegacyDaemon) throws
    /// Moves the plist aside into the helper's directory, or back to /Library/LaunchDaemons.
    func moveLegacyPlist(_ daemon: LegacyDaemon, aside: Bool) throws
    func bootstrap(_ daemon: LegacyDaemon) throws
}

public struct FanSnapshot: Sendable, Equatable {
    public struct Fan: Sendable, Equatable {
        public var manual: Bool
        public var target: Double
        public var min: Double
        public var max: Double

        public init(manual: Bool, target: Double, min: Double, max: Double) {
            self.manual = manual
            self.target = target
            self.min = min
            self.max = max
        }
    }

    public var fans: [Fan]
    /// Hottest CPU/GPU die, °C; nil when the sensors were not read.
    public var hottest: Double?

    public init(fans: [Fan], hottest: Double? = nil) {
        self.fans = fans
        self.hottest = hottest
    }

    /// Is the hardware still where the mode puts it? fanctl's `Fans.holds`: the SMC trims a held
    /// target by a few percent on its own, another app moves it far.
    public func holds(_ mode: FanMode) -> Bool {
        switch mode {
        case .auto:
            return fans.allSatisfy { !$0.manual }
        case .fixed(let percent):
            return fans.allSatisfy { fan in
                let range = fan.max - fan.min
                let wanted = fan.min + range * Double(percent.value) / 100
                return fan.manual && abs(fan.target - wanted) <= Swift.max(range * 0.15, 150)
            }
        }
    }
}

public struct ProcessFootprint: Sendable, Equatable {
    public var pid: Int32
    public var name: String
    public var bytes: UInt64

    public init(pid: Int32, name: String, bytes: UInt64) {
        self.pid = pid
        self.name = name
        self.bytes = bytes
    }
}

/// A failure of the system side, with what to show the caller.
public struct BackendError: Error, CustomStringConvertible {
    public var description: String

    public init(_ description: String) { self.description = description }
}
