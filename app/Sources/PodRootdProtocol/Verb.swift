/// Everything pod-rootd does, one case per verb (docs/pod-rootd.md, "Verbs"). Each names the state
/// it wants, so sending it twice changes nothing the second time.
public enum Verb: Codable, Hashable, Sendable {
    /// Read-only: the whole `Status`.
    case status
    case fansSet(mode: FanMode)
    /// Keeps `SleepDisabled` on while this session lasts, for at most `seconds`.
    case lidHold(seconds: LidSeconds)
    case lidRelease
    case powerMode(source: PowerSource, mode: PowerMode)
    case sysctlSet(setting: SysctlSetting, persist: Bool)
    case sysctlReset(key: SysctlKey)
    case shaperSet(interface: InterfaceName, kbps: UplinkKbps, scope: ShaperScope)
    case shaperClear(interface: InterfaceName)
    case spotlightAppsOnly
    case spotlightRestore
    case fsguardSet(enabled: Bool, limitMB: FSGuardLimitMB)
    case launchdParkOrphans(dryRun: Bool)
    case logsPruneDiagnostics(olderThanDays: DiagnosticAgeDays, dryRun: Bool)
    case legacyMigrate
    case legacyRollback
    case restoreDefaults

    /// Stable name for logs and the CLI.
    public var name: String {
        switch self {
        case .status: "status"
        case .fansSet: "fans.set"
        case .lidHold: "lid.hold"
        case .lidRelease: "lid.release"
        case .powerMode: "power.mode"
        case .sysctlSet: "sysctl.set"
        case .sysctlReset: "sysctl.reset"
        case .shaperSet: "shaper.set"
        case .shaperClear: "shaper.clear"
        case .spotlightAppsOnly: "spotlight.appsOnly"
        case .spotlightRestore: "spotlight.restore"
        case .fsguardSet: "fsguard.set"
        case .launchdParkOrphans: "launchd.parkOrphans"
        case .logsPruneDiagnostics: "logs.pruneDiagnostics"
        case .legacyMigrate: "legacy.migrate"
        case .legacyRollback: "legacy.rollback"
        case .restoreDefaults: "restoreDefaults"
        }
    }

    /// The verb with its parameters, for the log line.
    public var summary: String {
        switch self {
        case .fansSet(let mode): "\(name) \(mode)"
        case .lidHold(let seconds): "\(name) \(seconds)s"
        case .powerMode(let source, let mode): "\(name) \(source.rawValue) \(mode.name)"
        case .sysctlSet(let setting, let persist): "\(name) \(setting.key.rawValue)=\(setting.value)\(persist ? " persist" : "")"
        case .sysctlReset(let key): "\(name) \(key.rawValue)"
        case .shaperSet(let interface, let kbps, let scope): "\(name) \(interface) \(kbps)kbps \(scope.rawValue)"
        case .shaperClear(let interface): "\(name) \(interface)"
        case .fsguardSet(let enabled, let limit): "\(name) \(enabled ? "on" : "off") \(limit)MB"
        case .launchdParkOrphans(let dryRun): "\(name)\(dryRun ? " dry-run" : "")"
        case .logsPruneDiagnostics(let days, let dryRun): "\(name) \(days)d\(dryRun ? " dry-run" : "")"
        default: name
        }
    }

    public var kind: VerbKind {
        switch self {
        case .status: .read
        case .fansSet: .fans
        case .lidHold, .lidRelease, .powerMode: .power
        case .shaperSet, .shaperClear: .shaper
        case .fsguardSet: .config
        case .sysctlSet, .sysctlReset, .spotlightAppsOnly, .spotlightRestore, .launchdParkOrphans,
             .logsPruneDiagnostics, .legacyMigrate, .legacyRollback, .restoreDefaults:
            .system
        }
    }

    /// Tier A is harmless and rate-limited: anyone admitted may send it, no prompt. Tier B changes the
    /// system: Pod Menu sends it on a click (the click is the consent), `pod-rootctl` only with an
    /// approval (Touch ID, or a grace of 5 minutes for the same parent), Pod's Electron process never
    /// (docs/pod-rootd.md, "Tiers").
    public var tier: VerbTier {
        switch self {
        case .status, .fansSet, .lidHold, .lidRelease: .a
        // the hotspot controller's limit: it ends with its session, and 6 Mb/s (hotspot.py's floor)
        // is the slowest it can make the uplink; the helper also keeps it off an interface that has
        // a limit until reboot
        case .shaperSet(_, let kbps, .session) where kbps.value >= Self.sessionUplinkFloorKbps: .a
        default: .b
        }
    }

    /// The lowest session-scoped upload limit that is still tier A.
    public static let sessionUplinkFloorKbps: Int64 = 6000

    /// Every bounded parameter within its range. Decoding guarantees it; a verb built in code with
    /// `init(unchecked:)` might not.
    public var isValid: Bool {
        switch self {
        case .fansSet(.fixed(let percent)): percent.isValid
        case .lidHold(let seconds): seconds.isValid
        case .sysctlSet(.maxVnodes(let n), _): n.isValid
        case .sysctlSet(.gpuWiredLimit(.megabytes(let mb)), _): mb.isValid
        case .shaperSet(_, let kbps, _): kbps.isValid
        case .fsguardSet(_, let limit): limit.isValid
        case .logsPruneDiagnostics(let days, _): days.isValid
        default: true
        }
    }
}

public enum VerbTier: String, Codable, Sendable, CaseIterable {
    case a
    case b
}

/// Verb classes for the rate limits.
public enum VerbKind: String, Codable, Sendable, CaseIterable {
    case read
    case fans
    case power
    case shaper
    case config
    case system
}

/// What `pod-rootctl` says about the person behind a tier B verb. Only the CLI's own signed code
/// builds it, so the helper can trust it: `authenticated` after a LocalAuthentication approval in this
/// invocation, `parent` names the process that ran the CLI (session, parent pid and its start, tty)
/// for the grace window.
public struct Approval: Codable, Hashable, Sendable {
    public var authenticated: Bool
    public var parent: String

    public init(authenticated: Bool, parent: String) {
        self.authenticated = authenticated
        self.parent = parent
    }
}

/// One message to the helper.
public struct Request: Codable, Hashable, Sendable {
    public var version: Int
    public var verb: Verb
    /// Only from `pod-rootctl`, for tier B.
    public var approval: Approval?

    public init(_ verb: Verb, approval: Approval? = nil, version: Int = PodRootd.protocolVersion) {
        self.version = version
        self.verb = verb
        self.approval = approval
    }
}
