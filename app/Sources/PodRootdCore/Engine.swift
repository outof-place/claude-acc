import Foundation
import os
import PodRootdProtocol

/// One XPC session to the helper. Leases (a lid hold, a session-scoped upload limit) belong to it
/// and end with it.
nonisolated public struct SessionID: Hashable, Sendable, CustomStringConvertible {
    public let raw: UInt64

    public init(_ raw: UInt64) { self.raw = raw }

    public var description: String { "#\(raw)" }
}

enum Log {
    static let verb = Logger(subsystem: "codes.pod.rootd", category: "verb")
    static let engine = Logger(subsystem: "codes.pod.rootd", category: "engine")
    static let fsguard = Logger(subsystem: "codes.pod.rootd", category: "fsguard")
}

/// The helper's logic, on the main actor, against a `Backend`: the policy, the rate limits, the
/// state that survives a restart, the leases that don't, and the rules ported from fanctl's
/// `Daemon` and `Lid` and from fsguard.py. Every verb names a wanted state; applying it twice
/// changes nothing the second time.
public final class Engine {
    public static let thermalPause: Double = 15 * 60
    public static let fsguardEvery: Double = 60
    static let fsguardMinGap: Double = 300
    static let fsguardTrendEvery: Double = 3600
    static let fsguardTrendBigEvery: Double = 600
    static let fsguardTrendBigMB: Int64 = 512
    static let othersWarnBytes: UInt64 = 8 << 30
    /// The SMC rounds a limit to what ifconfig prints (10 kb/s); closer than this is the same rate.
    static let rateSlack: Int64 = 10
    /// A lid hold whose session ended without a release, or that the helper held when it restarted,
    /// keeps `SleepDisabled` this long for Pod Menu to hold it again: a reconnect must not let a
    /// closed-lid Mac sleep. A release, SIGTERM, low battery or heat end it at once.
    public static let lidRehold: Double = 60
    /// How often a session from Pod or Pod Menu may start a look for a newer helper in Pod.app.
    public static let updateEvery: Double = 3600

    public let backend: Backend
    private let store: StateStore
    private let now: () -> Double
    private let policy: VerbPolicy
    /// Looks for a newer helper in Pod.app (off in tests that don't test it).
    private let updates: Bool
    private var limiter: RateLimiter
    public private(set) var state = HelperState()
    private var savedState: HelperState?

    // fans: what the SMC was given and what the rules found
    private var fansApplied: FanMode?
    private var boosting = false
    private var conflict = false
    private var fanError: String?

    // the lid: the end of each session's hold
    private var leases: [SessionID: Double] = [:]
    private var cooledUntil: Double?
    private var lastRelease: String?
    /// The re-hold window after a dropped session or a restart, and the reason it ends with.
    private var rehold: (until: Double, reason: String)?

    /// When each CLI parent last authenticated a tier B verb (the grace window).
    private var approvals: [String: Double] = [:]

    /// Session-scoped upload limits and the session that owns each.
    private var shaperOwners: [String: SessionID] = [:]

    // the package's install: a newer helper from Pod.app, and the uninstall
    private var updateCheckedAt: Double?
    private var updateDue = false
    /// A newer helper is in place: the process exits non-zero and launchd starts the new one.
    public private(set) var restartForUpdate = false
    /// `helper.uninstall` went through: the next tick boots the job out.
    public private(set) var uninstalling = false

    // fsguard
    private var fsguardCheckedAt: Double?
    private var footprintMB: Int64?
    private var bigSeen: [String: Double] = [:]

    public init(
        backend: Backend, store: StateStore, now: @escaping () -> Double = { Date.now.timeIntervalSince1970 },
        policy: VerbPolicy = .standard, limits: [VerbKind: RateLimiter.Limit] = RateLimiter.standard,
        updates: Bool = true
    ) {
        self.backend = backend
        self.store = store
        self.now = now
        self.policy = policy
        self.updates = updates
        limiter = RateLimiter(limits: limits)
    }

    // MARK: Lifecycle

    /// At launch (boot, crash, the first connection after an idle exit): put back what the kernel
    /// and the SMC forgot, and let go of what only a live session may hold.
    public func start() {
        state = store.load() ?? HelperState()
        savedState = state
        let boot = backend.bootTime
        for (name, record) in state.sysctls {
            guard let key = SysctlKey(rawValue: name) else {
                state.sysctls[name] = nil
                continue
            }
            var record = record
            if record.boot != boot {
                // a new boot: the kernel is back on its default, which is the new original
                guard record.persist else {
                    state.sysctls[name] = nil
                    continue
                }
                record.original = (try? backend.sysctl(key)) ?? record.original
                record.boot = boot
                state.sysctls[name] = record
            }
            if record.persist, (try? backend.sysctl(key)) != record.value {
                do {
                    try backend.setSysctl(key, to: record.value)
                    Log.engine.notice("start: \(name, privacy: .public)=\(record.value) again")
                } catch {
                    Log.engine.error("start: \(name, privacy: .public)=\(record.value): \(error, privacy: .public)")
                }
            }
        }
        for (name, record) in state.shapers {
            // a limit lives on the adapter until a restart; a session's limit ended with its session
            if record.boot != boot {
                state.shapers[name] = nil
            } else if record.scope == .session, let interface = InterfaceName(name) {
                try? backend.setUplinkLimit(interface, kbps: record.previous)
                state.shapers[name] = nil
            }
        }
        if state.lidHeldByUs {
            // held before the restart: kept for the window in which Pod Menu holds it again
            rehold = (now() + Self.lidRehold, "helper restarted")
            evaluateLid(because: "helper restarted")
        }
        if case .fixed = state.fans { driveFans() }
        persist()
    }

    /// SIGTERM (shutdown, unregister, the Login Items switch): fans and leases go back now; what is
    /// persisted comes back at the next start.
    public func shutdown() {
        if case .fixed = fansApplied, !conflict {
            try? backend.applyFans(.auto)
            fansApplied = .auto
        }
        leases.removeAll()
        rehold = nil
        evaluateLid(because: "helper stopped")
        for (name, _) in shaperOwners {
            if let interface = InterfaceName(name) { _ = try? clearShaper(interface) }
        }
        persist()
    }

    /// Nothing to watch: the process may exit and let launchd start it on the next connection.
    public var isIdle: Bool {
        state.fans == .auto && leases.isEmpty && !state.lidHeldByUs && !state.fsguard.enabled && shaperOwners.isEmpty
            && !updateDue && !uninstalling
    }

    /// Seconds until `tick` has work: 2 under a fixed fan setting (fanctl's tick), 5 while the lid is
    /// held, fsguard's minute; nil when nothing needs a clock.
    public var nextTickDelay: Double? {
        var delays: [Double] = []
        if updateDue || uninstalling { delays.append(0) }
        if case .fixed = state.fans { delays.append(2) }
        if !leases.isEmpty || state.lidHeldByUs { delays.append(5) }
        if state.fsguard.enabled {
            delays.append(max(0, Self.fsguardEvery - (now() - (fsguardCheckedAt ?? -.infinity))))
        }
        return delays.min()
    }

    public func tick() {
        if uninstalling { return backend.bootoutSelf() }
        if updateDue { selfUpdate() }
        if case .fixed = state.fans { driveFans() }
        if !leases.isEmpty || state.lidHeldByUs { evaluateLid(because: "expired") }
        fsguardTick()
        persist()
    }

    public func sessionEnded(_ session: SessionID) {
        if leases.removeValue(forKey: session) != nil {
            if leases.isEmpty, state.lidHeldByUs { rehold = (now() + Self.lidRehold, "session ended") }
            evaluateLid(because: "session ended")
            Log.engine.notice("session \(session.description, privacy: .public) ended: lid lease dropped")
        }
        for (name, owner) in shaperOwners where owner == session {
            guard let interface = InterfaceName(name) else { continue }
            do {
                _ = try clearShaper(interface)
                Log.engine.notice("session \(session.description, privacy: .public) ended: limit on \(name, privacy: .public) cleared")
            } catch {
                Log.engine.error("session end: clear \(name, privacy: .public): \(error, privacy: .public)")
            }
        }
        persist()
    }

    // MARK: Requests

    /// One message: `caller` is nil when the sender matched no allowed identity, `request` nil when
    /// it did not decode (a parameter out of range fails here).
    public func handle(_ request: Request?, from caller: Caller?, session: SessionID) -> Reply {
        guard let caller else {
            Log.verb.error("refused session \(session.description, privacy: .public): sender matches no allowed signing identity")
            return Reply(.refused(.peerNotAllowed))
        }
        if updates, caller != .cli, now() - (updateCheckedAt ?? -.infinity) >= Self.updateEvery { updateDue = true }
        guard let request else {
            Log.verb.error("\(caller.rawValue, privacy: .public): refused a request that did not decode")
            return Reply(.refused(.invalid("the request did not decode (unknown verb or a parameter out of range)")))
        }
        guard request.version == PodRootd.protocolVersion else {
            Log.verb.error("\(caller.rawValue, privacy: .public): protocol version \(request.version), helper speaks \(PodRootd.protocolVersion)")
            return Reply(.refused(.versionMismatch(helper: PodRootd.protocolVersion)))
        }
        let verb = request.verb
        let outcome: Outcome
        if !verb.isValid {
            outcome = .refused(.invalid("a parameter is out of range"))
        } else if !policy.permits(tier(of: verb, session: session, caller: caller), for: caller) {
            outcome = .refused(.verbNotAllowed(verb: verb.name, caller: caller))
        } else if !approved(request, tier: tier(of: verb, session: session, caller: caller), from: caller) {
            outcome = .refused(.needsApproval(verb: verb.name))
        } else if let after = limiter.take(verb.kind, for: caller, now: now()) {
            outcome = .refused(.rateLimited(retryAfter: after))
        } else {
            outcome = perform(verb, session: session)
        }
        persist()
        let line = Self.describe(outcome)
        if verb == .status {
            Log.verb.debug("\(caller.rawValue, privacy: .public) \(session.description, privacy: .public) status -> \(line, privacy: .public)")
        } else {
            Log.verb.notice("\(caller.rawValue, privacy: .public) \(session.description, privacy: .public) \(verb.summary, privacy: .public) -> \(line, privacy: .public)")
        }
        return Reply(outcome, status: status())
    }

    /// Tier B from a caller that needs an approval: authenticated in this request (which starts the
    /// grace for its parent), or within the grace for the same parent. The CLI's signed code is what
    /// builds the approval; nothing else can send as the CLI.
    private func approved(_ request: Request, tier: VerbTier, from caller: Caller) -> Bool {
        guard tier == .b, policy.approvalNeeded.contains(caller) else { return true }
        let t = now()
        approvals = approvals.filter { t - $0.value <= policy.grace }
        guard let approval = request.approval, !approval.parent.isEmpty else { return false }
        if approval.authenticated {
            approvals[approval.parent] = t
            return true
        }
        return approvals[approval.parent] != nil
    }

    /// `Verb.tier`, with what only the state knows about upload limits: a session's limit may not
    /// cover one set until reboot (its end would not bring that one back), and clearing the limit of
    /// your own session, or a limit that isn't there, is as harmless as setting it. A lid hold from
    /// Pod's Electron process is B: anything can run that binary as Node, and a closed-lid hold
    /// keeps a Mac awake in a bag.
    func tier(of verb: Verb, session: SessionID, caller: Caller) -> VerbTier {
        switch verb {
        case .lidHold where caller == .app:
            return .b
        case .shaperSet(let interface, _, .session) where verb.tier == .a:
            return state.shapers[interface.name]?.scope == .untilReboot ? .b : .a
        case .shaperClear(let interface):
            if shaperOwners[interface.name] == session { return .a }
            if state.shapers[interface.name] == nil, backend.uplinkLimit(interface) == nil { return .a }
            return .b
        default:
            return verb.tier
        }
    }

    static func describe(_ outcome: Outcome) -> String {
        switch outcome {
        case .done(let changed, let note, _): (changed ? "changed" : "unchanged") + (note.map { ": " + $0 } ?? "")
        case .refused(let refusal): "refused, \(refusal)"
        }
    }

    private func perform(_ verb: Verb, session: SessionID) -> Outcome {
        do {
            switch verb {
            case .status:
                return .done(changed: false, note: nil, report: nil)
            case .fansSet(let mode):
                return setFans(mode)
            case .lidHold(let seconds):
                return done(holdLid(seconds, session: session))
            case .lidRelease:
                return done(releaseLid(session: session))
            case .powerMode(let source, let mode):
                return done(try setPower(mode, on: source))
            case .sysctlSet(let setting, let persist):
                return done(try setSysctl(setting, persist: persist))
            case .sysctlReset(let key):
                return done(try resetSysctl(key))
            case .shaperSet(let interface, let kbps, let scope):
                return done(try setShaper(interface, kbps: kbps, scope: scope, session: session))
            case .shaperClear(let interface):
                return done(try clearShaper(interface))
            case .spotlightAppsOnly:
                return done(try spotlightAppsOnly())
            case .spotlightRestore:
                return try spotlightRestore()
            case .fsguardSet(let enabled, let limit):
                return done(setFSGuard(enabled: enabled, limit: limit))
            case .launchdParkOrphans(let dryRun):
                let found = backend.launchdOrphans()
                if !dryRun { for orphan in found { try backend.park(orphan) } }
                return .done(changed: !dryRun && !found.isEmpty, note: nil, report: .orphans(found, parked: !dryRun))
            case .logsPruneDiagnostics(let days, let dryRun):
                let (files, bytes) = backend.pruneDiagnostics(olderThanDays: days.value, dryRun: dryRun)
                return .done(changed: !dryRun && files > 0, note: nil, report: .pruned(files: files, bytes: bytes, dryRun: dryRun))
            case .legacyMigrate:
                return try migrate()
            case .legacyRollback:
                return try rollback()
            case .restoreDefaults:
                return try restoreDefaults()
            case .helperUninstall:
                return try uninstall()
            }
        } catch let refusal as Refusal {
            return .refused(refusal)
        } catch {
            return .refused(.failed("\(error)"))
        }
    }

    private func done(_ changed: Bool) -> Outcome { .done(changed: changed, note: nil, report: nil) }

    // MARK: Fans

    private func setFans(_ mode: FanMode) -> Outcome {
        let before = (state.fans, fansApplied, conflict)
        state.fans = mode
        if conflict {
            // a pick made while another app holds the fans takes them back, even the same mode
            conflict = false
            fansApplied = nil
        }
        if mode == .auto { boosting = false }
        driveFans()
        if let fanError { return .refused(.failed(fanError)) }
        return done(before.0 != mode || before.1 != fansApplied || before.2)
    }

    /// fanctl `Daemon.tick`: write the SMC only when the target changes or a fixed setting was lost
    /// to sleep; another app setting the fans after us is a conflict, reported, not fought; a chip at
    /// 95 °C under a fixed setting gets full speed until it is below 85 °C.
    private func driveFans() {
        let mode = state.fans
        var measured = false
        if case .fixed(let percent) = mode, percent.value < 100 { measured = true }
        let snapshot: FanSnapshot
        do {
            snapshot = try backend.fanSnapshot(sensors: measured)
        } catch {
            fanError = "\(error)"
            return
        }
        if measured, let hottest = snapshot.hottest {
            if hottest >= 95 { boosting = true } else if hottest < 85 { boosting = false }
        }
        if mode == .auto { boosting = false }
        let target: FanMode = mode != .auto && boosting ? .fixed(FanPercent(unchecked: 100)) : mode
        if target != fansApplied {
            writeFans(target)
        } else if !conflict, !snapshot.holds(target) {
            if case .fixed = target, snapshot.fans.allSatisfy({ !$0.manual }) {
                writeFans(target)  // the SMC forgot a fixed setting over sleep
            } else {
                conflict = true
                Log.engine.notice("fans: another app set them after us, not fighting it")
            }
        }
    }

    private func writeFans(_ target: FanMode) {
        do {
            try backend.applyFans(target)
            fansApplied = target
            fanError = nil
        } catch {
            fanError = "\(error)"
            try? backend.applyFans(.auto)
            fansApplied = .auto
            Log.engine.error("fans: \(target.description, privacy: .public) failed, back to auto: \(error, privacy: .public)")
        }
    }

    // MARK: Lid

    private func holdLid(_ seconds: LidSeconds, session: SessionID) -> Bool {
        let before = state.lidHeldByUs
        leases[session] = now() + Double(seconds.value)
        rehold = nil
        evaluateLid(because: "released")
        return before != state.lidHeldByUs
    }

    private func releaseLid(session: SessionID) -> Bool {
        guard leases.removeValue(forKey: session) != nil else { return false }
        let before = state.lidHeldByUs
        if leases.isEmpty { rehold = nil }
        evaluateLid(because: "released")
        return before != state.lidHeldByUs
    }

    /// fanctl `Lid.tick`: hold `SleepDisabled` while a lease is live, the battery is above 10% and
    /// the Mac is not hot; turn it off only when we turned it on (someone else's setting stays). The
    /// re-hold window only keeps a hold of ours, it never takes one.
    private func evaluateLid(because reason: String) {
        let t = now()
        if backend.thermalSerious() { cooledUntil = t + Self.thermalPause }
        leases = leases.filter { $0.value > t }
        if !leases.isEmpty { rehold = nil }
        let keeping = leases.isEmpty && state.lidHeldByUs && t < (rehold?.until ?? -.infinity)
        let lowBattery = (!leases.isEmpty || keeping) && backend.batteryLow()
        let cooling = t < (cooledUntil ?? -.infinity)
        let wanted = (!leases.isEmpty || keeping) && !lowBattery && !cooling
        // a window that ran out ends the hold with the window's reason
        let reason = rehold?.reason ?? reason
        if !keeping { rehold = nil }
        let disabled = backend.sleepDisabled()
        if wanted, !state.lidHeldByUs, !disabled {
            do {
                try backend.setSleepDisabled(true)
                state.lidHeldByUs = true
            } catch {
                Log.engine.error("lid: SleepDisabled on: \(error, privacy: .public)")
            }
        } else if !wanted, state.lidHeldByUs {
            do {
                try backend.setSleepDisabled(false)
                state.lidHeldByUs = false
                lastRelease = lowBattery ? "battery" : cooling ? "thermal" : reason
            } catch {
                Log.engine.error("lid: SleepDisabled off: \(error, privacy: .public)")
            }
        } else if state.lidHeldByUs, !disabled {
            state.lidHeldByUs = false  // turned off by hand: taken again on the next tick if still wanted
            lastRelease = "turned off by hand"
        }
    }

    // MARK: Power mode

    private func setPower(_ mode: PowerMode, on source: PowerSource) throws -> Bool {
        if mode == .high, !backend.highPowerCapable { throw Refusal.unsupported("this Mac has no high power mode") }
        let current = backend.powerMode(source)
        guard current != mode else { return false }
        if state.powerOriginal[source.rawValue] == nil, let current { state.powerOriginal[source.rawValue] = current }
        try backend.setPowerMode(mode, on: source)
        if state.powerOriginal[source.rawValue] == mode { state.powerOriginal[source.rawValue] = nil }
        return true
    }

    // MARK: Sysctls

    private func setSysctl(_ setting: SysctlSetting, persist: Bool) throws -> Bool {
        let key = setting.key
        let value = setting.value
        if case .gpuWiredLimit(.megabytes(let mb)) = setting, mb.value > backend.memoryMB - 4096 {
            throw Refusal.invalid("\(mb.value) MB leaves the system less than 4 GB of \(backend.memoryMB) MB")
        }
        let current = try backend.sysctl(key)
        let record = state.sysctls[key.rawValue]
        if current != value {
            try backend.setSysctl(key, to: value)
            guard (try? backend.sysctl(key)) == value else {
                try? backend.setSysctl(key, to: current)
                throw Refusal.failed("the kernel did not take \(key.rawValue)=\(value)")
            }
        }
        let original = record?.original ?? current
        if value == original, !persist {
            state.sysctls[key.rawValue] = nil
        } else {
            state.sysctls[key.rawValue] = SysctlRecord(original: original, value: value, persist: persist, boot: backend.bootTime)
        }
        return current != value || (record?.persist ?? false) != persist
    }

    private func resetSysctl(_ key: SysctlKey) throws -> Bool {
        guard let record = state.sysctls[key.rawValue] else {
            // no record of ours: only the GPU limit has a known default to go back to
            if key == .gpuWiredLimitMB, try backend.sysctl(key) != 0 {
                try backend.setSysctl(key, to: 0)
                return true
            }
            return false
        }
        if try backend.sysctl(key) != record.original { try backend.setSysctl(key, to: record.original) }
        state.sysctls[key.rawValue] = nil
        return true
    }

    // MARK: Upload limit

    private static func sameRate(_ a: Int64?, _ b: Int64?) -> Bool {
        guard let a, let b else { return a == nil && b == nil }
        return abs(a - b) <= max(rateSlack, b / 1000)
    }

    private func setShaper(_ interface: InterfaceName, kbps: UplinkKbps, scope: ShaperScope, session: SessionID) throws -> Bool {
        guard backend.interfaceExists(interface) else { throw Refusal.unsupported("no interface \(interface)") }
        let current = backend.uplinkLimit(interface)
        let record = state.shapers[interface.name]
        let moved = !Self.sameRate(current, kbps.value)
        if moved { try backend.setUplinkLimit(interface, kbps: kbps.value) }
        state.shapers[interface.name] = ShaperRecord(
            kbps: kbps.value, previous: record.map(\.previous) ?? current, scope: scope, boot: backend.bootTime)
        if scope == .session { shaperOwners[interface.name] = session } else { shaperOwners[interface.name] = nil }
        return moved || record?.scope != scope
    }

    private func clearShaper(_ interface: InterfaceName) throws -> Bool {
        shaperOwners[interface.name] = nil
        guard let record = state.shapers.removeValue(forKey: interface.name) else {
            if backend.interfaceExists(interface), backend.uplinkLimit(interface) != nil {
                try backend.setUplinkLimit(interface, kbps: nil)
                return true
            }
            return false
        }
        if backend.interfaceExists(interface), !Self.sameRate(backend.uplinkLimit(interface), record.previous) {
            try backend.setUplinkLimit(interface, kbps: record.previous)
        }
        return true
    }

    // MARK: Spotlight

    private func spotlightAppsOnly() throws -> Bool {
        let wanted = try backend.appsOnlyExclusions()
        let current = try backend.spotlightExclusions()
        guard current != wanted else {
            defer { state.spotlightApplied = true }
            return !state.spotlightApplied
        }
        if state.spotlightSaved == nil { state.spotlightSaved = current }  // only the list from before
        try backend.setSpotlightExclusions(wanted)
        backend.reloadSpotlight()
        state.spotlightApplied = true
        return true
    }

    private func spotlightRestore() throws -> Outcome {
        guard let saved = state.spotlightSaved else {
            let was = state.spotlightApplied
            state.spotlightApplied = false
            return .done(changed: was, note: was ? "no list from before apps-only was saved; nothing to put back" : nil, report: nil)
        }
        if try backend.spotlightExclusions() != saved {
            try backend.setSpotlightExclusions(saved)
            backend.reloadSpotlight()
        }
        state.spotlightSaved = nil
        state.spotlightApplied = false
        return done(true)
    }

    // MARK: fsguard

    private func setFSGuard(enabled: Bool, limit: FSGuardLimitMB) -> Bool {
        let before = state.fsguard
        state.fsguard.enabled = enabled
        state.fsguard.limitMB = limit.value
        if !enabled {
            state.fsguard.overSince = nil
            footprintMB = nil
        } else if !before.enabled {
            fsguardCheckedAt = nil  // first check on the next tick
        }
        return before != state.fsguard
    }

    /// fsguard.py `Guard.run`: two readings in a row over the limit, at least 5 minutes after the
    /// last restart, restart fseventsd; log the trend, and any other process over 8 GB.
    private func fsguardTick() {
        guard state.fsguard.enabled else { return }
        let t = now()
        if let at = fsguardCheckedAt, t - at < Self.fsguardEvery { return }
        fsguardCheckedAt = t
        guard let target = backend.footprint(ofProcessNamed: "fseventsd") else {
            Log.fsguard.notice("fseventsd: no process or no reading")
            return
        }
        let mb = Int64(target.bytes >> 20)
        footprintMB = mb
        let every = mb > Self.fsguardTrendBigMB ? Self.fsguardTrendBigEvery : Self.fsguardTrendEvery
        if t - (state.fsguard.lastTrend ?? -.infinity) >= every {
            state.fsguard.lastTrend = t
            Log.fsguard.notice("fseventsd \(mb) MB")
        }
        if mb <= state.fsguard.limitMB {
            state.fsguard.overSince = nil
        } else if state.fsguard.overSince == nil {
            state.fsguard.overSince = t
            Log.fsguard.notice("fseventsd \(mb) MB over the limit of \(self.state.fsguard.limitMB) MB, restart at the next reading")
        } else if t - (state.fsguard.lastRestart ?? -.infinity) < Self.fsguardMinGap {
            Log.fsguard.notice("fseventsd \(mb) MB over the limit, but the last restart was moments ago")
        } else {
            Log.fsguard.notice("RESTART fseventsd pid \(target.pid) at \(mb) MB (limit \(self.state.fsguard.limitMB) MB)")
            backend.terminate(pid: target.pid, named: "fseventsd", grace: 10)
            state.fsguard.overSince = nil
            state.fsguard.lastRestart = t
            state.fsguard.restarts += 1
            state.fsguard.generation += 1
        }
        // only a log: another process over 8 GB is a lead for later, not a reason to kill it
        for process in backend.processes(over: Self.othersWarnBytes) {
            let key = "\(process.pid):\(process.name)"
            if t - (bigSeen[key] ?? -.infinity) >= Self.fsguardTrendEvery {
                bigSeen[key] = t
                Log.fsguard.notice("note: \(process.name, privacy: .public) (pid \(process.pid)) \(process.bytes >> 20) MB")
            }
        }
        bigSeen = bigSeen.filter { t - $0.value <= 6 * Self.fsguardTrendEvery }
    }

    // MARK: The five old daemons

    /// The old daemons the helper takes over today. The hotspot daemon stays until hotspot.py runs its
    /// controller as the user and sets the limit through `shaper.set` (docs/pod-rootd.md).
    public static let migrating: [LegacyDaemon] = [.fans, .fsguard, .iogpu, .vnodes]

    private func migrate() throws -> Outcome {
        let present = Self.migrating.compactMap { daemon in backend.legacyArguments(daemon).map { (daemon, $0) } }
        guard !present.isEmpty else { return .done(changed: false, note: nil, report: .legacy([])) }
        var fans: FanMode?
        var vnodes: Int64?
        var gpu: Int64?
        var fsguard = false
        var notes: [String] = []
        for (daemon, arguments) in present {
            switch daemon {
            case .vnodes: vnodes = Self.sysctlValue("kern.maxvnodes", in: arguments)
            case .iogpu: gpu = Self.sysctlValue("iogpu.wired_limit_mb", in: arguments)
            case .fans:
                if let i = arguments.firstIndex(of: "--config"), i + 1 < arguments.count {
                    fans = backend.legacyFanMode(configPath: arguments[i + 1])
                }
            case .fsguard: fsguard = true
            case .hotspot: break  // not in `migrating` yet
            }
            try backend.bootout(daemon)
            try backend.moveLegacyPlist(daemon, aside: true)
            if !state.legacy.contains(daemon) { state.legacy.append(daemon) }
        }
        if state.spotlightSaved == nil, let list = backend.legacySpotlightList() {
            state.spotlightSaved = list
            state.spotlightApplied = true
        }
        // the same settings, now the helper's
        if let fans, case .refused(let why) = setFans(fans) { notes.append("fans: \(why)") }
        if let vnodes {
            if let n = MaxVnodes(vnodes) {
                _ = try setSysctl(.maxVnodes(n), persist: true)
            } else {
                notes.append("kern.maxvnodes=\(vnodes) is outside \(MaxVnodes.allowed), left to the kernel")
            }
        }
        if let gpu, gpu > 0 {
            if let mb = GPUMegabytes(gpu), gpu <= backend.memoryMB - 4096 {
                _ = try setSysctl(.gpuWiredLimit(.megabytes(mb)), persist: true)
                // the old daemon's value is not what macOS had: the GPU limit's default is 0
                state.sysctls[SysctlKey.gpuWiredLimitMB.rawValue]?.original = 0
            } else {
                notes.append("iogpu.wired_limit_mb=\(gpu) is not allowed on this Mac, left to the kernel")
            }
        }
        if fsguard { _ = setFSGuard(enabled: true, limit: FSGuardLimitMB(unchecked: state.fsguard.limitMB)) }
        let migrated = present.map(\.0)
        return .done(changed: true, note: notes.isEmpty ? nil : notes.joined(separator: "; "), report: .legacy(migrated))
    }

    private static func sysctlValue(_ name: String, in arguments: [String]) -> Int64? {
        arguments.lazy.compactMap { argument -> Int64? in
            guard argument.hasPrefix(name + "=") else { return nil }
            return Int64(argument.dropFirst(name.count + 1))
        }.first
    }

    private func rollback() throws -> Outcome {
        let migrated = state.legacy
        guard !migrated.isEmpty else { return .done(changed: false, note: nil, report: .legacy([])) }
        for daemon in migrated {
            switch daemon {
            case .fans:
                state.fans = .auto
                driveFans()
            case .vnodes: state.sysctls[SysctlKey.maxVnodes.rawValue] = nil  // the old daemon sets it at bootstrap
            case .iogpu: state.sysctls[SysctlKey.gpuWiredLimitMB.rawValue] = nil
            case .fsguard: _ = setFSGuard(enabled: false, limit: FSGuardLimitMB(unchecked: state.fsguard.limitMB))
            case .hotspot: break
            }
            try backend.moveLegacyPlist(daemon, aside: false)
            try backend.bootstrap(daemon)
            state.legacy.removeAll { $0 == daemon }
        }
        return .done(changed: true, note: nil, report: .legacy(migrated))
    }

    // MARK: Uninstall

    /// Everything back to how macOS had it (docs/pod-rootd.md, "Uninstall and restore"); the old
    /// daemons' backups stay, `legacy.rollback` is its own step.
    private func restoreDefaults() throws -> Outcome {
        var changed = false
        var failures: [String] = []
        func attempt(_ what: String, _ body: () throws -> Bool) {
            do { changed = try body() || changed } catch { failures.append("\(what): \(error)") }
        }
        attempt("fans") {
            guard state.fans != .auto || fansApplied != .auto else { return false }
            if case .refused(let why) = setFans(.auto) { throw why }
            return true
        }
        attempt("lid") {
            let held = state.lidHeldByUs || !leases.isEmpty
            leases.removeAll()
            rehold = nil
            evaluateLid(because: "restored")
            return held
        }
        for name in state.sysctls.keys.sorted() {
            guard let key = SysctlKey(rawValue: name) else { continue }
            attempt(name) { try resetSysctl(key) }
        }
        for name in state.shapers.keys.sorted() {
            guard let interface = InterfaceName(name) else { continue }
            attempt("shaper \(name)") { try clearShaper(interface) }
        }
        attempt("spotlight") {
            guard state.spotlightApplied || state.spotlightSaved != nil else { return false }
            if case .refused(let why) = try spotlightRestore() { throw why }
            return true
        }
        for (name, original) in state.powerOriginal.sorted(by: { $0.key < $1.key }) {
            guard let source = PowerSource(rawValue: name) else { continue }
            attempt("power \(name)") {
                if backend.powerMode(source) != original { try backend.setPowerMode(original, on: source) }
                state.powerOriginal[name] = nil
                return true
            }
        }
        attempt("fsguard") { setFSGuard(enabled: false, limit: .standard) }
        guard failures.isEmpty else { throw Refusal.failed(failures.joined(separator: "; ")) }
        return done(changed)
    }

    /// `restoreDefaults`, then the package's files: a restore that fails removes nothing. The old
    /// daemons' backups stay for `legacy.rollback` by hand.
    private func uninstall() throws -> Outcome {
        _ = try restoreDefaults()
        try backend.removeInstall()
        let kept = state.legacy
        state = HelperState()
        uninstalling = true
        Log.engine.notice("uninstall: defaults back, files removed; the job goes next")
        let note = kept.isEmpty ? nil
            : "the old daemons stay off; their plists are kept in the helper's directory: "
            + kept.map(\.rawValue).joined(separator: ", ")
        return .done(changed: true, note: note, report: nil)
    }

    // MARK: Updates

    /// docs/pod-rootd.md, "Updates": a genuine copy in Pod.app with a higher version replaces the
    /// installed helper. A copy no newer than this one, or one that fails the check, is thrown away.
    private func selfUpdate() {
        updateDue = false
        updateCheckedAt = now()
        guard let own = backend.ownVersion else { return }
        for candidate in backend.updateCandidates() {
            let staged: String
            do {
                staged = try backend.stageUpdate(from: candidate)
            } catch {
                Log.engine.error("update: \(candidate, privacy: .public): \(error, privacy: .public)")
                continue
            }
            guard let version = backend.verifiedVersion(ofStaged: staged), Self.newer(version, than: own) else {
                backend.discardUpdate(staged)
                continue
            }
            do {
                try backend.installUpdate(staged)
            } catch {
                backend.discardUpdate(staged)
                Log.engine.error("update: install \(version, privacy: .public): \(error, privacy: .public)")
                continue
            }
            restartForUpdate = true
            Log.engine.notice("update: \(own, privacy: .public) -> \(version, privacy: .public) from \(candidate, privacy: .public)")
            return
        }
    }

    /// Dotted numbers, component by component; a version that isn't one is never newer.
    static func newer(_ version: String, than other: String) -> Bool {
        func parts(_ text: String) -> [Int]? {
            let parts = text.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
            return parts.isEmpty || parts.contains(nil) ? nil : parts.compactMap { $0 }
        }
        guard let a = parts(version), let b = parts(other) else { return false }
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0
            let y = i < b.count ? b[i] : 0
            if x != y { return x > y }
        }
        return false
    }

    // MARK: Status

    public func status() -> Status {
        var status = Status()
        status.helperVersion = backend.ownVersion
        status.fans.mode = state.fans
        status.fans.applied = fansApplied
        status.fans.boosting = boosting
        status.fans.conflict = conflict
        status.fans.error = fanError
        status.lid.heldByUs = state.lidHeldByUs
        status.lid.sleepDisabled = backend.sleepDisabled()
        status.lid.leaseUntil = leases.values.max()
        status.lid.cooledUntil = cooledUntil.flatMap { $0 > now() ? $0 : nil }
        status.lid.reholdUntil = state.lidHeldByUs && leases.isEmpty ? rehold.flatMap { $0.until > now() ? $0.until : nil } : nil
        status.lid.lastRelease = lastRelease
        status.power.ac = backend.powerMode(.ac)
        status.power.battery = backend.powerMode(.battery)
        status.power.highPowerCapable = backend.highPowerCapable
        status.power.originalAC = state.powerOriginal[PowerSource.ac.rawValue]
        status.power.originalBattery = state.powerOriginal[PowerSource.battery.rawValue]
        status.sysctls = SysctlKey.allCases.map { key in
            let record = state.sysctls[key.rawValue]
            return SysctlStatus(
                key: key, current: try? backend.sysctl(key), original: record?.original,
                persisted: record?.persist == true ? record?.value : nil)
        }
        status.shapers = state.shapers.keys.sorted().compactMap { name in
            guard let record = state.shapers[name] else { return nil }
            let current = InterfaceName(name).flatMap { backend.interfaceExists($0) ? backend.uplinkLimit($0) : nil }
            return ShaperStatus(
                interface: name, kbps: record.kbps, previousKbps: record.previous, scope: record.scope, currentKbps: current)
        }
        status.spotlight.appsOnly = state.spotlightApplied
        status.spotlight.savedEntries = state.spotlightSaved?.count
        status.fsguard.enabled = state.fsguard.enabled
        status.fsguard.limitMB = state.fsguard.limitMB
        status.fsguard.footprintMB = footprintMB
        status.fsguard.checkedAt = fsguardCheckedAt
        status.fsguard.restarts = state.fsguard.restarts
        status.fsguard.lastRestart = state.fsguard.lastRestart
        status.fsguard.generation = state.fsguard.generation
        status.legacy = LegacyDaemon.allCases.map {
            LegacyStatus(daemon: $0, installed: backend.legacyArguments($0) != nil, migrated: state.legacy.contains($0))
        }
        return status
    }

    // MARK: Persistence

    private func persist() {
        // the state file went with the uninstall
        guard state != savedState, !uninstalling else { return }
        do {
            try store.save(state)
            savedState = state
        } catch {
            Log.engine.error("state not saved: \(error, privacy: .public)")
        }
    }
}
