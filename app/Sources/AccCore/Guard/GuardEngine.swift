// The guard's loop in acc-cored, in place of `acc.py devguard run`: the same tick on the same
// cadence (interval_seconds, 2 s while memory is tight), plus a tick as soon as the kernel reports
// memory pressure. Everything that acts stays Python's: a tick whose plan or brake is due is handed
// to `acc.py devguard once` (which reads the Mac again and acts on its own reading), the caps to
// `acc.py devguard caps`. In shadow mode nothing is handed over and the state goes to a file of its own.
// A devguard.json value of a type the native tick would read differently hands every tick over.
import Darwin
import Dispatch

/// Which tick goes to Python, and the waits after a handover. Python's loop runs on every tick
/// without a process start; a handover costs a cold Python, so two kinds of handover wait:
/// - a plan Python may decline on its own reading (`declinable`) that it did decline: `holdSeconds`,
///   while the plans under it and the brake still go;
/// - a brake, whatever its outcome (no victim, or a state Python couldn't save): brake()'s own gap
///   (`emergency_cooldown_seconds` at stage 3, else `brake_cooldown_seconds`).
/// Every other plan is tried again on the next tick: Python's reading may differ for a moment
/// (pressure flickering), and it acts as soon as it agrees.
public struct HandoverPolicy {
    public static let holdSeconds = 60.0
    public private(set) var heldUntil: [String: Double] = [:]
    public private(set) var brakeHeldUntil = 0.0

    public init() {}

    public struct Handover {
        public var plan: Plan?
        public var brake: Bool
        public var reason: String { plan.map { "\($0.action) \($0.key)" } ?? "brake" }
    }

    static func key(_ plan: Plan) -> String { plan.key + "|" + plan.action + "|" + plan.code }

    /// The first actable plan that isn't held, and the brake when it's due and not waiting.
    public func next(actable: [Plan], brakeDue: Bool, now: Double) -> Handover? {
        let plan = actable.first { (heldUntil[Self.key($0)] ?? 0) <= now }
        let brake = brakeDue && brakeHeldUntil <= now
        return plan != nil || brake ? Handover(plan: plan, brake: brake) : nil
    }

    /// After Python's tick: `acted` when it stopped, restarted, shut down or warned something.
    public mutating func record(_ handover: Handover, acted: Bool, brakeGap: Double, now: Double) {
        heldUntil = heldUntil.filter { $0.value > now }
        if let plan = handover.plan, !acted, declinable(plan) { heldUntil[Self.key(plan)] = now + Self.holdSeconds }
        if handover.brake { brakeHeldUntil = now + brakeGap }
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

    /// The snapshot of the last tick, for the scheduler running in the same process
    public func latestSnapshot() -> PyObject? {
        queue.sync { state["snapshot"]?.object }
    }

    public func stop() {
        queue.sync {
            timer?.cancel()
            pressureSource?.cancel()
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
        if probesFactory == nil { Profile.measure("host") { refreshHost() } }
        let probes = probesFactory?() ?? LiveProbes(home: options.home, orca: client, cache: procCache)
        let enforce = !options.shadow && cfg["mode"] == .string("enforce")
        let mismatched = cfg.mismatched
        if mismatched != mismatchLogged {
            mismatchLogged = mismatched
            if !mismatched.isEmpty {
                GuardLog.write("acc-cored: devguard.json ma wartości innego typu niż domyślne (\(mismatched.joined(separator: ", "))): każdy przebieg robi Python")
            }
        }
        var next = state
        GuardLog.hold()
        let result = guardTick(cfg: cfg, state: &next, orca: orca, probes: probes, enforce: enforce, shaper: options.shadow ? nil : shaper)
        lastTick = probes.now
        ticks += 1
        // the first tick reads every process's arguments and asks every pattern once: not the loop's cost
        if ticks == 1, Profile.on { Profile.reset() }
        let handover = options.shadow ? nil
            : !mismatched.isEmpty ? HandoverPolicy.Handover(plan: nil, brake: false) : policy.next(actable: result.actable, brakeDue: result.brakeDue, now: probes.now)
        if let handover {
            GuardLog.release(write: false)  // Python's tick logs and notifies from its own reading
            handOver(handover, result: result, cfg: cfg, configOnly: !mismatched.isEmpty)
        } else {
            GuardLog.release(write: true)
            state = next
            save()
        }
        // check_caps runs in Python's loop on every enforcing tick (`enforce and qos`)
        if enforce, capsDue(cfg, now: probes.now) { runCaps() }
        guard reschedule else { return }
        let stage = state["snapshot"]?["pressure"]?["stage"]?.int ?? 0
        let interval = cfg.number("interval_seconds")
        schedule(after: max(0.5, stage >= 1 ? min(interval, 2) : interval))
    }

    /// The tick goes to Python: the state as it was before this tick, the lock let go, `devguard once`,
    /// then the state Python saved. Python's tick re-reads the Mac, decides and acts.
    func handOver(_ handover: HandoverPolicy.Handover, result: GuardTickResult, cfg: GuardConfig, configOnly: Bool) {
        save()
        let before = actedStamp(state)
        if lockFd >= 0 { _ = Self.flockRetrying(lockFd, LOCK_UN) }
        // a hung tick (an Orca call that never answers) must not stop the guard for good
        let argv = options.python + ["devguard", "once"]
        let rc = runner?(argv) ?? Spawn.run(argv, log: nil, timeout: Self.onceTimeout)
        if lockFd >= 0 { _ = Self.flockRetrying(lockFd, LOCK_EX) }
        handovers += 1
        state = Files.json(statePath, fallback: .object(state)).object ?? state
        let reason = configOnly ? "config" : handover.reason
        if rc != 0 { GuardLog.write("acc-cored: devguard once wyszedł z kodem \(rc) (\(reason))") }
        guard !configOnly else { return }
        let s = LastResort.settings(cfg)
        let gap = (result.world.pressure.stage >= 3 ? s["emergency_cooldown_seconds"] : s["brake_cooldown_seconds"])?.double ?? 0
        policy.record(handover, acted: actedStamp(state) != before, brakeGap: gap, now: result.world.now)
    }

    static let onceTimeout = 120.0

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
        guard stamp != hostStamp || (client == nil && now >= hostRetryAt) else { return }
        hostStamp = stamp
        client = HostResolver.resolve(python: options.python, home: options.home)
        if client == nil { hostRetryAt = now + 60 }
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

