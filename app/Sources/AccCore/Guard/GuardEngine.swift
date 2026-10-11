// The guard's loop in acc-cored, in place of `acc.py devguard run`: the same tick on the same
// cadence (interval_seconds, 2 s while memory is tight), plus a tick as soon as the kernel reports
// memory pressure. Everything that acts stays Python's: a tick whose plan or brake is due is handed
// to `acc.py devguard once` (which reads the Mac again and acts on its own reading), the caps to
// `acc.py devguard caps`. In shadow mode nothing is handed over and the state goes to a file of its own.
// A devguard.json value of a type the native tick would read differently hands the guard to Python's
// own loop (`devguard run`, QoS shaping and caps included) until the file reads natively again.
import Darwin
import Dispatch

/// Which tick goes to Python, and the waits after a handover. Python's loop runs on every tick
/// without a process start; a handover costs a cold Python, so two kinds of handover wait:
/// - a plan Python may decline on its own reading (`declinable`) that it did decline: `holdSeconds`,
///   while the plans under it and the brake still go;
/// - a brake, whatever its outcome (no victim, or a state Python couldn't save): brake()'s own gap
///   for the stage it is at now (`emergency_cooldown_seconds` at stage 3, else
///   `brake_cooldown_seconds`), so pressure climbing to stage 3 shortens the wait at once; a stage
///   above the last brake's goes at once (stage 3 picks victims stage 2 can't: Python's brake_at
///   moved only if stage 2 reaped, and then brakeDue already waits for it).
/// Every other plan is tried again on the next tick: Python's reading may differ for a moment
/// (pressure flickering), and it acts as soon as it agrees.
public struct HandoverPolicy {
    public static let holdSeconds = 60.0
    public private(set) var heldUntil: [String: Double] = [:]
    /// when the last brake went to Python, and at which stage
    public private(set) var lastBrake = -Double.infinity
    public private(set) var lastBrakeStage = 0

    public init() {}

    public struct Handover {
        public var plan: Plan?
        public var brake: Bool
        public var reason: String { plan.map { "\($0.action) \($0.key)" } ?? "brake" }
    }

    static func key(_ plan: Plan) -> String { plan.key + "|" + plan.action + "|" + plan.code }

    /// The first actable plan that isn't held, and the brake when it's due and either the stage
    /// rose since the last brake handover or its gap (for the stage the tick is at) has passed.
    public func next(actable: [Plan], brakeDue: Bool, now: Double, brakeGap: Double, stage: Int) -> Handover? {
        let plan = actable.first { (heldUntil[Self.key($0)] ?? 0) <= now }
        let brake = brakeDue && (stage > lastBrakeStage || now - lastBrake >= brakeGap)
        return plan != nil || brake ? Handover(plan: plan, brake: brake) : nil
    }

    /// After Python's tick: `acted` when it stopped, restarted, shut down or warned something.
    public mutating func record(_ handover: Handover, acted: Bool, now: Double, stage: Int) {
        heldUntil = heldUntil.filter { $0.value > now }
        if let plan = handover.plan, !acted, declinable(plan) { heldUntil[Self.key(plan)] = now + Self.holdSeconds }
        if handover.brake {
            lastBrake = now
            lastBrakeStage = stage
        }
    }

    /// brake()'s gap at a stage: emergency_cooldown_seconds at 3, brake_cooldown_seconds below
    public static func brakeGap(_ cfg: GuardConfig, stage: Int) -> Double {
        let s = LastResort.settings(cfg)
        return (stage >= 3 ? s["emergency_cooldown_seconds"] : s["brake_cooldown_seconds"])?.double ?? 0
    }
}

public final class GuardEngine: @unchecked Sendable {
    public struct Options: Sendable {
        public var home: String
        /// observe only: no lock, no handover, no QoS changes, the state to `shadowPath`
        public var shadow: Bool
        public var shadowPath: String?
        /// the interpreter and launcher the scripts run with ($STATE/python, $STATE/acc.py)
        public var python: [String]

        public init(home: String, shadow: Bool, shadowPath: String? = nil, python: [String]? = nil) {
            self.home = home
            self.shadow = shadow
            self.shadowPath = shadowPath
            let state = GuardPaths.state(home)
            self.python = python ?? [state + "/python", "-B", state + "/acc.py"]
        }
    }

    let options: Options
    // Python's guard runs as ProcessType Interactive: under a heavy load this queue must not starve
    let queue = DispatchQueue(label: "acc-cored.guard", qos: .userInitiated)
    var timer: DispatchSourceTimer?
    var pressureSource: DispatchSourceMemoryPressure?
    var lockFd: Int32 = -1
    var state = PyObject()
    var orca = OrcaView()
    var client: OrcaClient?
    var hostStamp = ""
    /// orcahost.py failed (a timeout under load): when to ask again
    var hostRetryAt = 0.0
    /// orcahost.py runs off the guard's queue (20 s timeout): one at a time
    var resolving = false
    /// tests: the host lookup in place of orcahost.py
    var resolver: (@Sendable () -> OrcaClient?)?
    /// Python's own loop while devguard.json doesn't read natively, and the pid of a test stand-in
    var pythonLoop: pid_t?
    var loopStarter: (([String]) -> pid_t?)?
    /// when the loop last started, and when it may start again: a loop that ends within a minute
    /// (a config Python can't run either) waits 10 s, doubling to 5 min, as launchd would throttle it
    var loopStartedAt = 0.0
    var loopRestartAt = 0.0
    var loopShortRuns = 0
    /// the lock was let go for Python's loop (alive, ended, or waiting out its backoff): native
    /// ticks again only after taking it back
    var pythonMode = false
    var lockWaitLogged = false
    /// the first host lookup answered (or failed): until then nobody counts as watched, so no shaping
    var hostSettled = false
    var shaper = QoSShaper()
    let procCache = ProcCache()
    var lastTick = 0.0
    public private(set) var policy = HandoverPolicy()
    /// tests: the readings, Python's `once` and the caps run in place of the Mac's and the scripts'
    var probesFactory: (() -> GuardProbes)?
    var runner: (([String]) -> Int32)?
    var capsRunner: (() -> Void)?
    /// the devguard.json keys last found of the wrong type (logged when they change)
    var mismatchLogged: [String] = []
    var capsRunning = false
    public private(set) var ticks = 0
    public private(set) var handovers = 0

    var statePath: String { GuardPaths.state(options.home) + "/devguard-state.json" }
    var configPath: String { GuardPaths.state(options.home) + "/devguard.json" }
    var lockPath: String { GuardPaths.state(options.home) + "/devguard.lock" }

    public init(_ options: Options) {
        self.options = options
        GuardLog.path = options.shadow ? nil : GuardPaths.state(options.home) + "/devguard.log"
        GuardLog.notifications = !options.shadow
        GuardContext.host = GuardPatterns.Host(appNames: HostResolver.appNames(Spawn.currentEnv()))
    }

    /// Takes the guard's lock (unless shadow) and starts ticking; false when another guard holds it.
    public func start() -> Bool {
        if !options.shadow {
            Files.mkdirs(GuardPaths.state(options.home))
            lockFd = open(lockPath, O_WRONLY | O_CREAT | O_CLOEXEC, 0o644)
            guard lockFd >= 0, Self.flockRetrying(lockFd, LOCK_EX | LOCK_NB) else {
                if lockFd >= 0 { close(lockFd) }
                lockFd = -1
                return false
            }
        }
        state = Files.json(options.shadow ? (options.shadowPath ?? statePath) : statePath, fallback: .object(PyObject())).object ?? PyObject()
        GuardLog.write("start strażnika (acc-cored)")
        let source = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: queue)
        source.setEventHandler { [weak self] in self?.pressureEvent() }
        source.resume()
        pressureSource = source
        queue.async { self.loop() }
        return true
    }

    public func stop() {
        queue.sync {
            timer?.cancel()
            pressureSource?.cancel()
            // Python's loop goes first: an orphan would keep devguard.lock and guard on its own
            endPythonLoop()
            if !options.shadow { shaper.restoreAll() }
            GuardLog.write("koniec strażnika (acc-cored)")
            if lockFd >= 0 {
                _ = Self.flockRetrying(lockFd, LOCK_UN)
                close(lockFd)
                lockFd = -1
            }
        }
    }

    /// flock through EINTR
    static func flockRetrying(_ fd: Int32, _ op: Int32) -> Bool {
        while true {
            if flock(fd, op) == 0 { return true }
            if errno != EINTR { return false }
        }
    }

    /// The kernel says memory got tight: tick now rather than at the next interval.
    func pressureEvent() {
        if Kernel.wall() - lastTick >= 1 { loop() }
    }

    func schedule(after seconds: Double) {
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(flags: [], queue: queue)
        t.schedule(deadline: .now() + seconds, leeway: .milliseconds(Int(seconds * 100)))
        t.setEventHandler { [weak self] in self?.loop() }
        t.resume()
        timer = t
    }

    /// One tick without scheduling the next (the bench)
    public func benchTick() { queue.sync { loop(reschedule: false) } }

    /// One pass of cmd_run's loop: config, foreign writes, the tick, the save, the next wake.
    func loop(reschedule: Bool = true) {
        let cfg = Profile.measure("config") { GuardConfig.load(path: configPath) }
        Profile.measure("merge") { mergeForeign() }
        if probesFactory == nil || resolver != nil {
            Profile.measure("host") { refreshHost() }
        } else {
            hostSettled = true
        }
        let mismatched = cfg.mismatched
        if mismatched != mismatchLogged {
            mismatchLogged = mismatched
            if !mismatched.isEmpty {
                GuardLog.write("acc-cored: devguard.json ma wartości innego typu niż domyślne (\(mismatched.joined(separator: ", "))): strażnika prowadzi pętla Pythona")
            }
        }
        // a config the native tick can't read the way Python does: Python's own loop, until it can
        if !options.shadow, !mismatched.isEmpty {
            // a caps run in flight finishes first: Python's loop would run its own check_caps beside it
            if !capsRunning { runPythonLoop() }
            if reschedule { schedule(after: interval(cfg)) }
            return
        }
        // back from Python's loop: only with the lock in hand and Python's last state
        if pythonMode, !leavePythonMode() {
            if reschedule { schedule(after: interval(cfg)) }
            return
        }
        let probes = probesFactory?() ?? LiveProbes(home: options.home, orca: client, cache: procCache)
        let enforce = !options.shadow && cfg["mode"] == .string("enforce")
        var next = state
        GuardLog.hold()
        // before the first host answer nobody counts as watched: shaping would push the focused server to the E cores
        let result = guardTick(cfg: cfg, state: &next, orca: orca, probes: probes, enforce: enforce, shaper: options.shadow || !hostSettled ? nil : shaper)
        lastTick = probes.now
        ticks += 1
        // the first tick reads every process's arguments and asks every pattern once: not the loop's cost
        if ticks == 1, Profile.on { Profile.reset() }
        let stage = result.world.pressure.stage
        let gap = HandoverPolicy.brakeGap(cfg, stage: stage)
        let handover = options.shadow ? nil : policy.next(actable: result.actable, brakeDue: result.brakeDue, now: probes.now, brakeGap: gap, stage: stage)
        if let handover {
            GuardLog.release(write: false)  // Python's tick logs and notifies from its own reading
            // another guard took the lock meanwhile: no save, no caps, until native has it back
            guard handOver(handover, result: result, cfg: cfg) else {
                if reschedule { schedule(after: interval(cfg)) }
                return
            }
        } else {
            GuardLog.release(write: true)
            state = next
            save()
        }
        // check_caps runs in Python's loop on every enforcing tick (`enforce and qos`); its caps_at
        // goes to the file now, so a Python loop taking over doesn't run the caps again at once
        if enforce, capsDue(cfg, now: probes.now) {
            save()
            runCaps()
        }
        guard reschedule else { return }
        let snapshotStage = state["snapshot"]?["pressure"]?["stage"]?.int ?? 0
        schedule(after: max(0.5, snapshotStage >= 1 ? min(interval(cfg), 2) : interval(cfg)))
    }

    /// interval_seconds, or its default when it isn't a number (Python's loop can't sleep on it either)
    func interval(_ cfg: GuardConfig) -> Double {
        let v = cfg["interval_seconds"]
        guard v.string == nil, let n = v.double, n > 0 else { return GuardConfig.defaults["interval_seconds"]?.double ?? 5 }
        // a timer's leeway is n * 100 ms in an Int: 1e17 would trap; 0.001 would spin the paths
        // that wait (a mismatched config, a lock held by another guard) at a kilohertz
        return max(0.5, min(n, 3600))
    }

    /// The tick goes to Python: the state as it was before this tick, the lock let go, `devguard once`,
    /// then the state Python saved. Python's tick re-reads the Mac, decides and acts. False when
    /// another guard (launchd's `devguard run`, a manual one) took the lock in that window: native
    /// doesn't wait for it here (the queue and stop() would hang on it) but goes to pythonMode, and
    /// leavePythonMode takes the lock back without waiting, with Python's state.
    func handOver(_ handover: HandoverPolicy.Handover, result: GuardTickResult, cfg: GuardConfig) -> Bool {
        save()
        let before = actedStamp(state)
        if lockFd >= 0 { _ = Self.flockRetrying(lockFd, LOCK_UN) }
        // a hung tick (an Orca call that never answers) must not stop the guard for good
        let argv = options.python + ["devguard", "once"]
        let rc = runner?(argv) ?? Spawn.run(argv, log: nil, timeout: Self.onceTimeout)
        handovers += 1
        state = Files.json(statePath, fallback: .object(state)).object ?? state
        if rc != 0 { GuardLog.write("acc-cored: devguard once wyszedł z kodem \(rc) (\(handover.reason))") }
        policy.record(handover, acted: actedStamp(state) != before, now: result.world.now, stage: result.world.pressure.stage)
        if lockFd >= 0, !Self.flockRetrying(lockFd, LOCK_EX | LOCK_NB) {
            pythonMode = true
            return false
        }
        return true
    }

    /// Python's slowest tick: a pool Simulator's shutdown (lsappinfo 2 × 5 s, running_apps 20 s,
    /// simctl shutdown 120 s) plus Orca's reads; killed mid-action it would lose its event
    static let onceTimeout = 300.0

    // MARK: Python's loop

    /// `devguard run` as a child, with the lock let go: a crash of acc-cored takes it along (same
    /// process group), and it is started again if it ends, after a wait that grows while it ends
    /// within a minute.
    func runPythonLoop() {
        let now = Kernel.wall()
        if let pid = pythonLoop {
            var status: Int32 = 0
            if waitpid(pid, &status, WNOHANG) == 0 { return }
            pythonLoop = nil
            backOff(now, ran: now - loopStartedAt)
            let how = status & 0x7f == 0 ? "kod \((status >> 8) & 0xff)" : "sygnał \(status & 0x7f)"
            GuardLog.write("acc-cored: pętla Pythona skończyła się (\(how)), następny start za \(Int(loopRestartAt - now)) s")
        }
        guard now >= loopRestartAt else { return }
        if lockFd >= 0 { _ = Self.flockRetrying(lockFd, LOCK_UN) }
        pythonMode = true
        // Python restores what it pinned itself; what native pinned goes back now, or the two caches lie
        shaper.restoreAll()
        shaper.forget()
        let argv = options.python + ["devguard", "run"]
        loopStartedAt = now
        pythonLoop = loopStarter.map { $0(argv) } ?? Spawn.start(argv, log: GuardPaths.state(options.home) + "/devguard-launchd.log")
        if pythonLoop == nil {
            backOff(now, ran: 0)
            GuardLog.write("acc-cored: pętla Pythona nie wystartowała (\(argv[0])), następna próba za \(Int(loopRestartAt - now)) s")
        }
    }

    /// 10 s after a loop that lived a minute or more; doubling to 5 min while it ends sooner (or
    /// doesn't start)
    func backOff(_ now: Double, ran: Double) {
        loopShortRuns = ran < 60 ? loopShortRuns + 1 : 0
        loopRestartAt = now + min(10 * pow(2, Double(max(loopShortRuns - 1, 0))), 300)
    }

    /// The config reads natively again: Python's loop ends, and native takes the lock back without
    /// waiting (another guard may hold it: `devguard run` from launchd, a manual `devguard once`);
    /// false while it can't, and native doesn't tick. Then Python's last state is the state.
    func leavePythonMode() -> Bool {
        endPythonLoop()
        if lockFd >= 0, !Self.flockRetrying(lockFd, LOCK_EX | LOCK_NB) {
            if !lockWaitLogged { GuardLog.write("acc-cored: devguard.lock trzyma inny strażnik, czekam") }
            lockWaitLogged = true
            return false
        }
        lockWaitLogged = false
        pythonMode = false
        loopShortRuns = 0
        loopRestartAt = 0
        state = Files.json(statePath, fallback: .object(state)).object ?? state
        savedStamp = Files.stamp(statePath)
        shaper.forget()
        return true
    }

    /// Python's loop, if it runs, ends (SIGTERM: it restores QoS and logs its end). After 10 s it is
    /// killed with what it started (simctl, orca): they aren't in a group of their own, so the whole
    /// tree is collected first and then killed (a child killed first would hand its own to launchd).
    /// Each process is stopped before its children are read: a running one could start another
    /// between the read and the kill, and that one would escape.
    func endPythonLoop() {
        guard let pid = pythonLoop else { return }
        pythonLoop = nil
        kill(pid, SIGTERM)
        let killed = Spawn.reap(pid, deadline: Kernel.wall() + 10, beforeKill: {
            kill(pid, SIGSTOP)
            var tree: [pid_t] = []
            var todo = [pid]
            while let p = todo.popLast() {
                for c in Proc.children(of: p) where c > 1 && !tree.contains(c) && c != pid {
                    kill(c, SIGSTOP)
                    tree.append(c)
                    todo.append(c)
                }
            }
            for c in tree { kill(c, SIGKILL) }
        }) == nil
        if killed { GuardLog.write("acc-cored: pętla Pythona nie skończyła się w 10 s, zabita") }
    }

    /// What changes when execute() acts: last_action (stop, restart, shutdown), the events, the warns.
    func actedStamp(_ s: PyObject) -> String {
        "\(s["last_action"]?.double ?? 0)|\(s["events"]?.array?.count ?? 0)|\(s["warned"].map { $0.dumps() } ?? "")"
    }

    /// save_state: writer and saved_at, then the file by rename.
    func save() {
        state["writer"] = .int(Int(getpid()))
        state["saved_at"] = .double(Kernel.wall())
        let path = options.shadow ? (options.shadowPath ?? statePath + ".native") : statePath
        let text = Profile.measure("dump") { PyJSON.object(state).dumps() }
        Profile.measure("write") { _ = Files.writeAtomic(path, text) }
        savedStamp = Files.stamp(path)
    }

    var savedStamp: Files.Stamp?

    /// merge_foreign: manual stop/recycle write the state beside the loop; their events, recycle count
    /// and last action must survive the loop's own copy.
    func mergeForeign() {
        guard !options.shadow, let stamp = Files.stamp(statePath), stamp != savedStamp else { return }
        guard let disk = Files.json(statePath, fallback: .null).object else { return }
        let writer = disk["writer"]
        if writer == nil || writer == .null || writer == .int(Int(getpid())) { return }
        if (disk["saved_at"]?.double ?? 0) <= (state["saved_at"]?.double ?? 0) { return }
        for key in ["events", "recycles", "pending"] {
            let mine = state[key]?.array ?? []
            let seen = Set(mine.map(\.sortedDump))
            let extra = (disk[key]?.array ?? []).filter { !seen.contains($0.sortedDump) }
            if !extra.isEmpty {
                let at: (PyJSON) -> Double = key == "recycles" ? { $0.array?.first?.double ?? 0 } : { $0["at"]?.double ?? 0 }
                let merged = stableSortedDouble(mine + extra, by: at)
                state[key] = .array(Array(merged.suffix(20)))
            }
        }
        state["last_action"] = .double(max(state["last_action"]?.double ?? 0, disk["last_action"]?.double ?? 0))
        savedStamp = stamp
    }

    // MARK: host

    /// Which host app (Orca or Pod) the guard reads: orcahost.py, once at start and again whenever
    /// owner.json or the set of running host apps changes.
    func refreshHost() {
        let owner = Files.stamp(GuardPaths.state(options.home) + "/owner.json").map { "\($0)" } ?? "-"
        let running = procCache.hostApps
        let stamp = owner + "|" + running
        let now = Kernel.wall()
        // a failed resolve (a 20 s timeout under load) is asked again a minute later, not only when
        // the host changes: without Orca there are no recycle plans
        guard !resolving, stamp != hostStamp || (client == nil && now >= hostRetryAt) else { return }
        hostStamp = stamp
        resolving = true
        let python = options.python, home = options.home, resolver = resolver
        DispatchQueue.global(qos: .utility).async {
            // a stub's nil is a failed lookup, not a reason to ask the live host
            let found = resolver.map { $0() } ?? HostResolver.resolve(python: python, home: home)
            self.queue.async {
                self.resolving = false
                self.hostSettled = true
                self.client = found
                if found == nil { self.hostRetryAt = Kernel.wall() + 60 }
            }
        }
    }

    // MARK: caps

    func capsDue(_ cfg: GuardConfig, now: Double) -> Bool {
        let every = cfg.number("caps_minutes") * MINUTE
        guard every > 0, !capsRunning, now - (state["caps_at"]?.double ?? 0) >= every else { return false }
        state["caps_at"] = .double(now)
        return true
    }

    func runCaps() {
        if let capsRunner {
            capsRunner()
            return
        }
        capsRunning = true
        let argv = options.python + ["devguard", "caps"]
        DispatchQueue.global(qos: .background).async { [weak self] in
            _ = Spawn.run(argv, log: nil, timeout: 1800)
            self?.capsFinished()
        }
    }

    func capsFinished() {
        queue.async { self.capsRunning = false }
    }
}

extension PyJSON {
    /// json.dumps(item, sort_keys=True), for merge_foreign's "seen" test
    var sortedDump: String {
        switch self {
        case .object(let o):
            return "{" + o.keys.sorted(by: pyLess).map { PyJSON.string($0).dumps() + ": " + o[$0]!.sortedDump }.joined(separator: ", ") + "}"
        case .array(let a): return "[" + a.map(\.sortedDump).joined(separator: ", ") + "]"
        default: return dumps()
        }
    }
}

extension Files {
    public struct Stamp: Equatable, CustomStringConvertible, Sendable {
        let ino: UInt64, size: Int64, mtime: Int, mtimeNs: Int
        public var description: String { "\(ino):\(size):\(mtime).\(mtimeNs)" }
    }

    public static func stamp(_ path: String) -> Stamp? {
        var st = stat()
        guard stat(path, &st) == 0 else { return nil }
        return Stamp(ino: st.st_ino, size: st.st_size, mtime: st.st_mtimespec.tv_sec, mtimeNs: st.st_mtimespec.tv_nsec)
    }
}

/// PRIO_DARWIN_BG for dev servers you don't look at (set_background), and back at exit.
public final class QoSShaper {
    /// pid -> (start, on): the kernel doesn't tell another process's flag
    var background: [pid_t: (UInt64, Bool)] = [:]

    public init() {}

    func set(_ pid: pid_t, start: UInt64, on: Bool) {
        if let known = background[pid], known == (start, on) { return }
        guard setpriority(PRIO_DARWIN_PROCESS, id_t(pid), on ? PRIO_DARWIN_BG : 0) == 0 else { return }
        background[pid] = (start, on)
    }

    /// shape(cfg, world)
    func shape(_ cfg: GuardConfig, _ world: GuardWorld, probes: GuardProbes) {
        for unit in world.units {
            unit.background = cfg.flag("background_unattended") && !unit.attended && !unit.protected
            for pid in unit.pids {
                if let info = probes.usage(pid), !world.table.facts(pid).sacred {
                    set(pid, start: info.start, on: unit.background)
                }
            }
        }
        for (pid, (start, _)) in background where !Proc.alive(pid, start: start) { background[pid] = nil }
    }

    /// restore_background: nothing stays pinned to the E cores after the guard exits
    public func restoreAll() {
        for (pid, (start, on)) in background where on && Proc.alive(pid, start: start) { set(pid, start: start, on: false) }
    }

    /// Another guard (Python's loop) sets the flags meanwhile: what this one remembers is stale.
    public func forget() {
        background = [:]
    }
}

/// orcahost: the host's names for the patterns, and the resolved host for the Orca reads.
public enum HostResolver {
    /// orcahost.app_names(env)
    public static func appNames(_ env: [String: String]) -> [String] {
        var names = ["Orca", "Pod"]
        for key in ["CLAUDE_ACC_HOST_APP", "POD_APP_PATH"] {
            if let app = env[key], !app.isEmpty {
                var trimmed = app.utf8[...]
                while trimmed.last == 0x2F { trimmed = trimmed.dropLast() }
                var name = basename(String(Substring(trimmed)))
                if name.pyEnds(".app") { name = String(name.unicodeScalars.dropLast(4).map(Character.init)) }
                if !name.isEmpty, !names.contains(name) { names.append(name) }
            }
        }
        return names
    }

    /// `orcahost.py` (a cold path: once at start and when the host may have changed)
    public static func resolve(python: [String], home: String) -> OrcaClient? {
        let script = GuardPaths.state(home) + "/orcahost.py"
        let argv = python.filter { !$0.hasSuffix("/acc.py") } + [script]
        guard let out = Spawn.output(argv, timeout: 20), let host = (try? PyJSON.loads(bytes: out))?.object else { return nil }
        guard let app = host["app"]?.string, let exe = host["executable"]?.string, let cli = host["cli"]?.string else { return nil }
        let marker = basename(app) + "/Contents/MacOS/" + exe
        var cliPath: String?
        if host["kind"]?.string != "orca" {
            let bundled = app + "/Contents/Resources/bin/" + cli
            if access(bundled, X_OK) == 0 { cliPath = bundled }
        }
        if cliPath == nil { cliPath = which(cli) }
        let runtime = host["runtime"]?.string ?? ((host["user_data"]?.string ?? "") + "/" + (host["runtime_file"]?.string ?? "orca-runtime.json"))
        return OrcaClient(marker: marker, runtimePath: runtime, cli: cliPath, env: GuardEnv.tools(home: home))
    }

    /// shutil.which over janitor.tool_path()'s PATH
    static func which(_ name: String) -> String? {
        for dir in (GuardEnv.tools(home: "")["PATH"] ?? "/usr/bin:/bin").split(separator: ":") {
            let path = String(dir) + "/" + name
            if access(path, X_OK) == 0 { return path }
        }
        return nil
    }
}

public enum GuardEnv {
    /// janitor.ENV: launchd's environment with janitor.tool_path() as PATH (the newest nvm node second)
    public static func tools(home: String) -> [String: String] {
        var env = Spawn.currentEnv()
        let h = home.isEmpty ? (env["HOME"] ?? "") : home
        var dirs = [h + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", h + "/.docker/bin", h + "/go/bin", h + "/.cargo/bin", h + "/.bun/bin"]
        let versions = (try? FileManagerLite.list(h + "/.nvm/versions/node")) ?? []
        let newest = versions.filter { Files.isDirectory(h + "/.nvm/versions/node/" + $0 + "/bin") }.max { a, b in
            numbers(a).lexicographicallyPrecedes(numbers(b))
        }
        if let newest { dirs.insert(h + "/.nvm/versions/node/" + newest + "/bin", at: 1) }
        dirs += ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env["PATH"] = dirs.filter(Files.isDirectory).joined(separator: ":")
        for k in ["HOMEBREW_NO_AUTO_UPDATE", "HOMEBREW_NO_ENV_HINTS", "HOMEBREW_NO_ANALYTICS", "HOMEBREW_NO_INSTALL_CLEANUP"] { env[k] = "1" }
        return env
    }

    /// re.findall(r"\d+", name) as ints
    static func numbers(_ s: String) -> [Int] {
        var out: [Int] = []
        var cur = ""
        for c in s {
            if c.isASCII, c.isNumber { cur.append(c) } else if !cur.isEmpty { out.append(Int(cur)!); cur = "" }
        }
        if !cur.isEmpty { out.append(Int(cur)!) }
        return out
    }
}

/// readdir without Foundation
enum FileManagerLite {
    static func list(_ dir: String) throws -> [String] {
        guard let d = opendir(dir) else { return [] }
        defer { closedir(d) }
        var out: [String] = []
        while let e = readdir(d) {
            let name = withUnsafeBytes(of: e.pointee.d_name) { raw in String(decoding: raw.prefix(Int(e.pointee.d_namlen)), as: UTF8.self) }
            if name != "." && name != ".." { out.append(name) }
        }
        return out
    }
}

