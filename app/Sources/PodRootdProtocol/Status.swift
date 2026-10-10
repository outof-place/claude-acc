/// What pod-rootd holds and what the system says now: the read-only side of every verb. Times are
/// seconds since 1970.
public struct Status: Codable, Hashable, Sendable {
    public var version = PodRootd.protocolVersion
    public var fans = FansStatus()
    public var lid = LidStatus()
    public var power = PowerStatus()
    public var sysctls: [SysctlStatus] = []
    public var shapers: [ShaperStatus] = []
    public var spotlight = SpotlightStatus()
    public var fsguard = FSGuardStatus()
    public var legacy: [LegacyStatus] = []

    public init() {}
}

public struct FansStatus: Codable, Hashable, Sendable {
    /// The mode asked for; persisted, so it comes back after a restart.
    public var mode: FanMode = .auto
    /// What the SMC was last given: the mode, or 100% while boosting.
    public var applied: FanMode?
    /// A chip reached 95 °C under a fixed setting: full speed until it is below 85 °C.
    public var boosting = false
    /// Another app set the fans after the helper did; it is reported, not fought.
    public var conflict = false
    public var error: String?

    public init() {}
}

public struct LidStatus: Codable, Hashable, Sendable {
    /// `SleepDisabled` is on because the helper turned it on.
    public var heldByUs = false
    /// `SleepDisabled` as the power domain has it, whoever set it.
    public var sleepDisabled = false
    /// The latest end of a live lease.
    public var leaseUntil: Double?
    /// After a thermal let-go the lid is not held again before this.
    public var cooledUntil: Double?
    /// Why the last hold ended: "released", "expired", "session ended", "battery", "thermal", ...
    public var lastRelease: String?

    public init() {}
}

public struct PowerStatus: Codable, Hashable, Sendable {
    public var ac: PowerMode?
    public var battery: PowerMode?
    public var highPowerCapable = false
    /// The modes before the helper's first change: what `restoreDefaults` puts back.
    public var originalAC: PowerMode?
    public var originalBattery: PowerMode?

    public init() {}
}

public struct SysctlStatus: Codable, Hashable, Sendable {
    public var key: SysctlKey
    public var current: Int64?
    /// The value before the helper's first change in this boot.
    public var original: Int64?
    /// Applied again at every boot.
    public var persisted: Int64?

    public init(key: SysctlKey, current: Int64?, original: Int64?, persisted: Int64?) {
        self.key = key
        self.current = current
        self.original = original
        self.persisted = persisted
    }
}

public struct ShaperStatus: Codable, Hashable, Sendable {
    public var interface: String
    public var kbps: Int64
    /// The limit before the helper's: what `shaper.clear` puts back (nil: none).
    public var previousKbps: Int64?
    public var scope: ShaperScope
    /// What the interface reports now (nil: no limit).
    public var currentKbps: Int64?

    public init(interface: String, kbps: Int64, previousKbps: Int64?, scope: ShaperScope, currentKbps: Int64?) {
        self.interface = interface
        self.kbps = kbps
        self.previousKbps = previousKbps
        self.scope = scope
        self.currentKbps = currentKbps
    }
}

public struct SpotlightStatus: Codable, Hashable, Sendable {
    public var appsOnly = false
    /// Entries of the Privacy list saved before apps-only, for `spotlight.restore`.
    public var savedEntries: Int?

    public init() {}
}

public struct FSGuardStatus: Codable, Hashable, Sendable {
    public var enabled = false
    public var limitMB: Int64 = FSGuardLimitMB.standard.value
    /// fseventsd's footprint at the last check.
    public var footprintMB: Int64?
    public var checkedAt: Double?
    public var restarts = 0
    public var lastRestart: Double?
    /// Goes up with every restart: user-side watchers (git fsmonitor, dev servers) react to it.
    public var generation = 0

    public init() {}
}

public struct LegacyStatus: Codable, Hashable, Sendable {
    public var daemon: LegacyDaemon
    /// Its plist is in /Library/LaunchDaemons.
    public var installed: Bool
    /// The helper moved it aside and took over; `legacy.rollback` puts it back.
    public var migrated: Bool

    public init(daemon: LegacyDaemon, installed: Bool, migrated: Bool) {
        self.daemon = daemon
        self.installed = installed
        self.migrated = migrated
    }
}
