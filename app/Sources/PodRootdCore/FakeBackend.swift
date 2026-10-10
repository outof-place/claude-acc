import PodRootdProtocol

/// The whole machine in memory, for the engine's tests: every write is recorded in `calls`, and
/// `failing` makes the named operation throw.
public final class FakeBackend: Backend {
    public var fans: [FanSnapshot.Fan] = [
        .init(manual: false, target: 2317, min: 2317, max: 6550),
        .init(manual: false, target: 2317, min: 2317, max: 6550),
    ]
    public var hottest: Double = 60
    public var sysctls: [SysctlKey: Int64] = [.maxVnodes: 263_168, .gpuWiredLimitMB: 0]
    /// A key the kernel takes but reads back unchanged.
    public var stubbornSysctls: Set<SysctlKey> = []
    public var memoryMB: Int64 = 49_152
    public var bootTime: Int64 = 1_791_505_097
    public var sleepIsDisabled = false
    public var powerModes: [PowerSource: PowerMode] = [.ac: .automatic, .battery: .automatic]
    public var highPowerCapable = true
    public var lowBattery = false
    public var hot = false
    public var interfaces: Set<String> = ["en0", "en8"]
    public var limits: [String: Int64] = [:]
    public var exclusions: [String] = ["/Users/x/Movies"]
    public var appsOnly: [String] = ["/Users/x/Documents", "/Users/x/Movies", "/Library"]
    public var spotlightReloads = 0
    public var footprints: [String: ProcessFootprint] = ["fseventsd": .init(pid: 321, name: "fseventsd", bytes: 40 << 20)]
    public var others: [ProcessFootprint] = []
    public var terminated: [Int32] = []
    public var orphans: [Orphan] = []
    public var parked: [Orphan] = []
    public var diagnostics: (files: Int, bytes: Int64) = (0, 0)
    /// The old plists in /Library/LaunchDaemons, with their ProgramArguments.
    public var legacyPlists: [LegacyDaemon: [String]] = [:]
    public var legacyAside: [LegacyDaemon: [String]] = [:]
    public var legacyBooted: Set<LegacyDaemon> = []
    public var fanConfigs: [String: FanMode] = [:]
    public var savedSpotlightList: [String]?
    public var savedSpotlightAside: [String]?
    /// A pod-rootd binary in a candidate place, as the self-update sees it.
    public struct Binary: Equatable, Sendable {
        public var version: String
        /// Pod's team, `codes.pod.rootd`, notarized.
        public var genuine: Bool
        public var symlink: Bool

        public init(version: String, genuine: Bool = true, symlink: Bool = false) {
            self.version = version
            self.genuine = genuine
            self.symlink = symlink
        }
    }

    public var installedVersion: String? = "56"
    public var candidates: [String: Binary] = [:]
    public var staged: [String: Binary] = [:]
    public var installed = true
    public var bootedOut = false
    public var failing: Set<String> = []
    public private(set) var calls: [String] = []

    public init() {}

    private func record(_ call: String) throws {
        if let name = call.split(separator: " ").first, failing.contains(String(name)) {
            throw BackendError("\(name) failed (fake)")
        }
        calls.append(call)
    }

    // MARK: Fans

    public func fanSnapshot(sensors: Bool) throws -> FanSnapshot {
        if failing.contains("fanSnapshot") { throw BackendError("SMC read failed (fake)") }
        return FanSnapshot(fans: fans, hottest: sensors ? hottest : nil)
    }

    public func applyFans(_ mode: FanMode) throws {
        try record("applyFans \(mode)")
        for i in fans.indices {
            switch mode {
            case .auto:
                fans[i].manual = false
                fans[i].target = fans[i].min
            case .fixed(let percent):
                fans[i].manual = true
                fans[i].target = fans[i].min + (fans[i].max - fans[i].min) * Double(percent.value) / 100
            }
        }
    }

    // MARK: Sysctls

    public func sysctl(_ key: SysctlKey) throws -> Int64 {
        guard let value = sysctls[key] else { throw BackendError("no sysctl \(key.rawValue)") }
        return value
    }

    public func setSysctl(_ key: SysctlKey, to value: Int64) throws {
        try record("setSysctl \(key.rawValue)=\(value)")
        if !stubbornSysctls.contains(key) { sysctls[key] = value }
    }

    // MARK: Power

    public func sleepDisabled() -> Bool { sleepIsDisabled }

    public func setSleepDisabled(_ on: Bool) throws {
        try record("setSleepDisabled \(on)")
        sleepIsDisabled = on
    }

    public func powerMode(_ source: PowerSource) -> PowerMode? { powerModes[source] }

    public func setPowerMode(_ mode: PowerMode, on source: PowerSource) throws {
        try record("setPowerMode \(source.rawValue) \(mode.name)")
        powerModes[source] = mode
    }

    public func batteryLow() -> Bool { lowBattery }

    public func thermalSerious() -> Bool { hot }

    // MARK: Upload limit

    public func interfaceExists(_ interface: InterfaceName) -> Bool { interfaces.contains(interface.name) }

    public func uplinkLimit(_ interface: InterfaceName) -> Int64? { limits[interface.name] }

    public func setUplinkLimit(_ interface: InterfaceName, kbps: Int64?) throws {
        try record("setUplinkLimit \(interface) \(kbps.map(String.init) ?? "off")")
        limits[interface.name] = kbps
    }

    // MARK: Spotlight

    public func spotlightExclusions() throws -> [String] { exclusions }

    public func setSpotlightExclusions(_ list: [String]) throws {
        try record("setSpotlightExclusions \(list.count)")
        exclusions = list
    }

    public func appsOnlyExclusions() throws -> [String] { appsOnly }

    public func reloadSpotlight() {
        calls.append("reloadSpotlight")
        spotlightReloads += 1
    }

    // MARK: fseventsd

    public func footprint(ofProcessNamed name: String) -> ProcessFootprint? { footprints[name] }

    public func terminate(pid: Int32, named name: String, grace: Double) {
        calls.append("terminate \(pid) \(name)")
        terminated.append(pid)
    }

    public func processes(over bytes: UInt64) -> [ProcessFootprint] { others.filter { $0.bytes > bytes } }

    // MARK: launchd and logs

    public func launchdOrphans() -> [Orphan] { orphans }

    public func park(_ orphan: Orphan) throws {
        try record("park \(orphan.label)")
        parked.append(orphan)
        orphans.removeAll { $0 == orphan }
    }

    public func pruneDiagnostics(olderThanDays days: Int64, dryRun: Bool) -> (files: Int, bytes: Int64) {
        calls.append("pruneDiagnostics \(days)\(dryRun ? " dry-run" : "")")
        defer { if !dryRun { diagnostics = (0, 0) } }
        return diagnostics
    }

    // MARK: Legacy

    public func legacyArguments(_ daemon: LegacyDaemon) -> [String]? { legacyPlists[daemon] }

    public func legacyFanMode(configPath: String) -> FanMode? { fanConfigs[configPath] }

    public func legacySpotlightList() -> [String]? { savedSpotlightList }

    public func moveLegacySpotlightList(aside: Bool) throws {
        try record("moveLegacySpotlightList \(aside ? "aside" : "back")")
        if aside {
            guard let list = savedSpotlightList else { throw BackendError("no list") }
            (savedSpotlightAside, savedSpotlightList) = (list, nil)
        } else {
            guard let list = savedSpotlightAside else { throw BackendError("no list aside") }
            (savedSpotlightList, savedSpotlightAside) = (list, nil)
        }
    }

    public func bootout(_ daemon: LegacyDaemon) throws {
        try record("bootout \(daemon.rawValue)")
        legacyBooted.remove(daemon)
    }

    public func moveLegacyPlist(_ daemon: LegacyDaemon, aside: Bool) throws {
        try record("moveLegacyPlist \(daemon.rawValue) \(aside ? "aside" : "back")")
        if aside {
            guard let arguments = legacyPlists.removeValue(forKey: daemon) else { throw BackendError("no plist") }
            legacyAside[daemon] = arguments
        } else {
            guard let arguments = legacyAside.removeValue(forKey: daemon) else { throw BackendError("no backup") }
            legacyPlists[daemon] = arguments
        }
    }

    public func bootstrap(_ daemon: LegacyDaemon) throws {
        try record("bootstrap \(daemon.rawValue)")
        legacyBooted.insert(daemon)
    }

    // MARK: The package's install

    public var ownVersion: String? { installedVersion }

    public func updateCandidates() -> [String] { candidates.keys.sorted() }

    public func stageUpdate(from candidate: String) throws -> String {
        try record("stageUpdate \(candidate)")
        guard let binary = candidates[candidate] else { throw BackendError("no such file") }
        guard !binary.symlink else { throw BackendError("\(candidate) is a symlink") }
        let path = "/staged/\(staged.count)"
        staged[path] = binary
        return path
    }

    public func verifiedVersion(ofStaged path: String) -> String? {
        calls.append("verify \(path)")
        guard let binary = staged[path], binary.genuine else { return nil }
        return binary.version
    }

    public func installUpdate(_ staged: String) throws {
        try record("installUpdate \(staged)")
        guard let binary = self.staged.removeValue(forKey: staged) else { throw BackendError("nothing staged") }
        installedVersion = binary.version
    }

    public func discardUpdate(_ staged: String) {
        calls.append("discardUpdate \(staged)")
        self.staged[staged] = nil
    }

    public func removeInstall() throws {
        try record("removeInstall")
        installed = false
    }

    public func bootoutSelf() {
        calls.append("bootoutSelf")
        bootedOut = true
    }
}
