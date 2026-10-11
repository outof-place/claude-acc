// accswitch's tick on acc-cored's clock: the 2-minute slots launchd uses, but a slot runs
// `acc.py tick` only when that tick could do something the last one didn't:
// - an input changed since the last tick ended: the login Keychain (its file, nothing in it),
//   accswitch's config, pause, usage cache and state, the jobs' held accounts, the host app's
//   account list and its account selection (orca-data.json);
// - the last tick wasn't quiet, or its deadline passed (state.json `tick_due`, which the tick
//   computes with its own functions: the active account's reading, its token, Depot's token);
// - `maxGap` passed since the last tick (the menu bar calls the switcher silent after 10 min).
// acc-cored never reads, refreshes or copies credentials: the tick stays Python's, and the
// Keychain is only a file stamp here.
//
// Nothing here ever signals a tick, as launchd never does: a tick killed between a token refresh
// and its Keychain write leaves a used refresh token behind, and the server answers its reuse by
// revoking the account. The tick leads a group of its own (it outlives acc-cored, launchd reaps
// it then), and its timeout is cooperative: `ACC_TICK_DEADLINE` tells it when to stop starting
// work, and it starts a refresh only with time left to finish it (accswitch.py `in_time`).
// The next slot is scheduled when the tick ends, so two never overlap.
//
// Every slot leaves a record in accswitch-slot.json: whether it ran the tick and why, the tick's
// exit code and time, and `checked_at`, the last slot that was a check: a skip, or a tick that
// ended with 0 (a tick that didn't start or crashed isn't one, so a dead switcher still goes
// "silent"). A skipped slot also names the status.json it vouches for (one the latest tick
// wrote): nothing a tick reads changed since that snapshot, so the menu bar counts it fresh.
//
// Shadow mode runs nothing. It watches launchd's own ticks and records, for each, whether this
// schedule would have run it and whether it changed anything (docs/acc-cored.md).
import Darwin
import Dispatch
import Synchronization

public final class SwitchEngine: @unchecked Sendable {
    public struct Options: Sendable {
        public var home: String
        public var python: [String]
        /// observe launchd's ticks instead of running them; the observations go to this file
        public var shadowPath: String?
        public var slot = 120.0
        public var maxGap = 480.0
        /// when, after its start, a tick stops starting work (ACC_TICK_DEADLINE); nothing kills it
        public var tickDeadline = 100.0

        public init(home: String, python: [String]? = nil, shadowPath: String? = nil) {
            self.home = home
            let state = GuardPaths.state(home)
            self.python = python ?? [state + "/python", "-B", state + "/acc.py"]
            self.shadowPath = shadowPath
        }
    }

    let options: Options
    let queue = DispatchQueue(label: "acc-cored.accswitch", qos: .utility)
    var timer: DispatchSourceTimer?
    var stateDir: String { GuardPaths.state(options.home) }
    /// the host app's data directory and settings file name (orcahost.py), resolved again when
    /// what the choice depends on changes (`hostWatch`), and every slot while it fails
    var host: HostResolver.DataFiles?
    var hostInputs: String?
    /// the inputs' stamps when the last tick ended; empty at start, so the first slot runs
    var baseline: [String: String] = [:]
    /// reasons the next slot runs whatever the inputs say: a change during the last tick, an odd exit
    var pending: [String] = []
    public private(set) var runs = 0
    public private(set) var skips = 0
    var stopped = false
    struct Running {
        var pid: pid_t
        var started: Double
        var before: [String: String]
        var lastTick: Double
        var reasons: [String]
    }
    /// the tick in flight
    var running: Running?
    /// the last slot that was a check (a skip, or a tick that ended with 0)
    var lastHealthy: Double?
    var hostFailureLogged = false
    /// what `ping` reads, without waiting for the engine's queue (an orcahost lookup may hold it)
    struct Published: Sendable {
        var runs = 0
        var skips = 0
        var tickStarted: Double?
        var lastSlot: String?
    }
    let published = Mutex(Published())
    /// tests: the tick's run in place of Python's (argv, its environment), on the engine's queue
    var runner: (([String], [String: String]) -> Int32)?
    /// tests: the host lookup in place of orcahost.py's
    var resolver: (() -> HostResolver.DataFiles?)?

    public init(_ options: Options) {
        self.options = options
    }

    // MARK: decisions

    /// What a tick reads and others may write, as stamps (inode, size, mtime). A missing file has
    /// none; the account list is one stamp of its names and their oauth-account.json files, the
    /// Keychain one of every keychain file (securityd replaces the one it writes by rename).
    func stamps() -> [String: String] {
        var out: [String: String] = [:]
        func add(_ name: String, _ path: String) { out[name] = Files.stamp(path).map { "\($0)" } ?? "-" }
        let keychains = options.home + "/Library/Keychains"
        let files = ((try? FileManagerLite.list(keychains)) ?? []).filter { $0.hasSuffix(".keychain-db") }.sorted()
        out["keychain"] = files.isEmpty ? Self.noKeychain : files.map { f in
            f + "=" + (Files.stamp(keychains + "/" + f).map { "\($0)" } ?? "-")
        }.joined(separator: ",")
        for f in ["config.json", "pause.json", "usage-cache.json", "state.json", "jobs/held-accounts.json"] {
            add(f, stateDir + "/" + f)
        }
        if let host {
            add("host settings", host.userData + "/" + host.dataFile)
            let profiles = host.userData + "/profiles"
            for p in ((try? FileManagerLite.list(profiles)) ?? []).sorted() {
                add("host settings " + p, profiles + "/" + p + "/" + host.dataFile)
            }
            let accounts = host.userData + "/claude-accounts"
            let ids = ((try? FileManagerLite.list(accounts)) ?? []).sorted()
            out["host accounts"] = ids.map { id in
                id + "=" + (Files.stamp(accounts + "/" + id + "/auth/oauth-account.json").map { "\($0)" } ?? "-")
            }.joined(separator: ",")
        }
        return out
    }

    public struct Decision: Equatable {
        public var run: Bool
        public var reasons: [String]
    }

    /// no keychain file to stamp: the main trigger is blind, so every slot runs
    static let noKeychain = "none"

    /// Whether the slot at `now` runs the tick: the inputs that changed since the baseline, the
    /// last tick's `tick_due`, the gap since it. A `last_tick` ahead of the clock (set back) runs
    /// the tick, which writes a true one: otherwise the gap would never come.
    static func decide(state: PyObject, changed: [String], now: Double, maxGap: Double) -> Decision {
        var reasons = changed.map { "changed: " + $0 }
        let lastTick = state["last_tick"]?.double ?? 0
        if lastTick > now + 5 { reasons.append("last tick ahead of the clock") }
        let due = state["tick_due"]?.object
        if due == nil {
            reasons.append("no tick_due")
        } else if !(due?["quiet"]?.truthy ?? false) {
            reasons.append("not quiet")
        } else if let at = due?["at"]?.double, at <= now {
            reasons.append("due: " + (due?["why"]?.string ?? ""))
        }
        // a slot may fire a little early; the gap counts from the tick's start, as the menu bar's
        if now - lastTick >= maxGap - 5 { reasons.append("gap") }
        return Decision(run: !reasons.isEmpty, reasons: reasons)
    }

    func changedInputs(_ now: [String: String]) -> [String] {
        // state.json changes with every tick; others may write it too (status --json, `token`), and
        // what they write can change the tick's path, so it counts like the rest
        Set(now.keys).union(baseline.keys).filter { now[$0] != baseline[$0] }.sorted()
    }

    // MARK: loop

    public func start() {
        queue.async {
            self.refreshHost()
            if self.options.shadowPath != nil {
                self.baseline = self.stamps()
                self.startShadow()
            } else {
                self.scheduleSlot()
            }
        }
    }

    /// Stops the slots without waiting: a tick in flight finishes on its own.
    public func stop() {
        queue.async {
            self.stopped = true
            self.timer?.cancel()
        }
    }

    /// What the slots did so far, for `ping`; never waits for the engine's queue.
    public func status() -> PyObject {
        let p = published.withLock { $0 }
        return PyObject([
            ("runs", .int(p.runs)), ("skips", .int(p.skips)),
            ("tick_running_s", p.tickStarted.map { .double((Kernel.wall() - $0).rounded()) } ?? .null),
            ("last_slot", p.lastSlot.flatMap { try? PyJSON.loads($0) } ?? .null),
        ])
    }

    /// The host as orcahost.py resolves it, asked again when one of the files the choice depends
    /// on changes (a host starting or quitting moves its SingletonLock) and every slot while it fails.
    func refreshHost() {
        let inputs = HostResolver.watchStamp(host?.watch ?? [])
        guard host == nil || inputs != hostInputs else { return }
        let found = resolver.map { $0() } ?? HostResolver.dataFiles(python: options.python, home: options.home)
        // one line when it starts failing, not one per slot
        if found == nil, !hostFailureLogged {
            Files.append(stateDir + "/launchd.log", "acc-cored: accswitch: orcahost.py didn't answer, the host's files go unwatched until it does\n")
        }
        hostFailureLogged = found == nil
        host = found
        hostInputs = HostResolver.watchStamp(found?.watch ?? [])
    }

    /// The next slot: `slot` after the last tick's start (launchd's cadence), at least a second away.
    func scheduleSlot() {
        guard !stopped else { return }
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        let now = Kernel.wall()
        let lastTick = min(state["last_tick"]?.double ?? 0, now)
        var next = lastTick + options.slot
        if next < now + 1 { next += ((now + 1 - next) / options.slot).rounded(.up) * options.slot }
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(wallDeadline: .now() + (next - now), leeway: .seconds(2))
        t.setEventHandler { [weak self] in self?.slotFired() }
        t.resume()
        timer = t
    }

    func slotFired() {
        guard !stopped, running == nil else { return }
        refreshHost()
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        let inputs = stamps()
        var decision = Self.decide(state: state, changed: changedInputs(inputs), now: Kernel.wall(), maxGap: options.maxGap)
        if inputs["keychain"] == Self.noKeychain { decision.reasons.append("no keychain file") }
        decision.reasons += pending
        decision.run = !decision.reasons.isEmpty
        pending = []
        guard decision.run else {
            skips += 1
            record(ran: false, reasons: [], rc: nil, seconds: nil)
            scheduleSlot()
            return
        }
        launch(Running(pid: 0, started: Kernel.wall(), before: inputs, lastTick: state["last_tick"]?.double ?? 0, reasons: decision.reasons))
    }

    /// Starts the tick with its cooperative deadline and waits for its end off the queue.
    func launch(_ run: Running) {
        let argv = options.python + ["accswitch", "tick"]
        var env = Spawn.currentEnv()
        env["ACC_TICK_DEADLINE"] = String(Int(run.started + options.tickDeadline))
        if let runner {
            running = run
            tickEnded(runner(argv, env))
            return
        }
        guard let pid = Spawn.start(argv, env: env, log: stateDir + "/launchd.log", ownGroup: true) else {
            // a tried slot that isn't a check: the menu bar says "silent" as it did under launchd
            Files.append(stateDir + "/launchd.log", "acc-cored: accswitch: the tick didn't start (\(argv[0]))\n")
            pending = ["the tick didn't start"]
            runs += 1
            record(ran: true, reasons: run.reasons, rc: 127, seconds: nil)
            scheduleSlot()
            return
        }
        var started = run
        started.pid = pid
        running = started
        published.withLock { $0.tickStarted = run.started }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            var status: Int32 = 0
            while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
            let rc = status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
            self?.queue.async { self?.tickEnded(rc) }
        }
    }

    /// The inputs the tick itself rewrites on every run: their change during it is its own
    static let tickOwn: Set<String> = ["state.json", "usage-cache.json"]

    func tickEnded(_ rc: Int32) {
        guard let run = running else { return }
        running = nil
        published.withLock { $0.tickStarted = nil }
        runs += 1
        let after = stamps()
        let lastTick = Files.json(stateDir + "/state.json", fallback: .null)["last_tick"]?.double ?? 0
        if lastTick > run.lastTick {
            // it ran (its first write is last_tick): what others wrote meanwhile isn't in its result,
            // so a change to another input arms the next slot, which settles after one more tick
            pending = Set(after.keys).union(run.before.keys)
                .filter { after[$0] != run.before[$0] && !Self.tickOwn.contains($0) }
                .sorted().map { "changed during the tick: " + $0 }
            baseline = after
        }
        // without a new last_tick the tick didn't get the lock: the old baseline keeps what was due
        if rc != 0 && rc != 1 { pending.append("the tick ended with \(rc)") }
        record(ran: true, reasons: run.reasons, rc: rc, seconds: Kernel.wall() - run.started)
        scheduleSlot()
    }

    /// accswitch-slot.json: this slot, the last check, and for a skipped one the status.json it vouches for.
    func record(ran: Bool, reasons: [String], rc: Int32?, seconds: Double?) {
        let now = Kernel.wall().rounded()
        if ran ? rc == 0 : rc == nil { lastHealthy = now }
        var statusAt: Double?
        if !ran {
            // only the latest tick's snapshot: a tick that didn't get to write it left an older one
            let generated = Files.json(stateDir + "/status.json", fallback: .null)["generated_at"]?.double
            let lastTick = Files.json(stateDir + "/state.json", fallback: .null)["last_tick"]?.double ?? 0
            if let generated, generated >= lastTick { statusAt = generated }
        }
        let rec = PyObject([
            ("at", .double(now)), ("ran", .bool(ran)), ("reasons", .array(reasons.map { .string($0) })),
            ("rc", rc.map { .int(Int($0)) } ?? .null), ("tick_s", seconds.map { .double(($0 * 10).rounded() / 10) } ?? .null),
            ("checked_at", lastHealthy.map { .double($0) } ?? .null),
            ("status_at", statusAt.map { .double($0) } ?? .null), ("runs", .int(runs)), ("skips", .int(skips)),
        ])
        let text = PyJSON.object(rec).dumps()
        published.withLock {
            $0.runs = runs
            $0.skips = skips
            $0.lastSlot = text
        }
        _ = Files.writeAtomic(stateDir + "/accswitch-slot.json", text)
    }

    // MARK: shadow

    struct Observed {
        var tickAt: Double
        var decision: Decision
        /// the inputs when the tick started, and the state it started from
        var start: [String: String]
        var logSize: Int64
        var activeTs: Double?
    }

    var observed: Observed?
    var lastSeenTick = 0.0
    var lastStatus = ""
    /// the state the last tick left: its tick_due and what others wrote since are the slot's inputs
    var previousState = PyObject()

    func startShadow() {
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        lastSeenTick = state["last_tick"]?.double ?? 0
        virtualTick = lastSeenTick
        previousState = state
        lastStatus = Files.stamp(stateDir + "/status.json").map { "\($0)" } ?? "-"
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 0.25, repeating: 0.25, leeway: .milliseconds(50))
        t.setEventHandler { [weak self] in self?.shadowPoll() }
        t.resume()
        timer = t
    }

    /// state.json without what every tick writes at its start and end
    static func comparable(_ state: PyObject) -> PyObject {
        var s = state
        for key in ["last_tick", "tick_due"] { s[key] = nil }
        return s
    }

    func shadowPoll() {
        // state.json is parsed only when its stamp moved
        let stateStamp = Files.stamp(stateDir + "/state.json").map { "\($0)" } ?? "-"
        if stateStamp != lastStateStamp {
            lastStateStamp = stateStamp
            shadowState = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        }
        let state = shadowState
        let tick = state["last_tick"]?.double ?? 0
        if tick > lastSeenTick {
            // a tick started (its first write is last_tick): decide as the slot would have
            lastSeenTick = tick
            let now = stamps()
            var changed = changedInputs(now).filter { $0 != "state.json" }
            if Self.comparable(state) != Self.comparable(previousState) { changed.append("state.json") }
            var decisionState = previousState
            if decisionState["tick_due"] == nil { decisionState["tick_due"] = estimatedDue(previousState).map { .object($0) } }
            // the gap counts from the last tick this schedule would have run, not launchd's
            decisionState["last_tick"] = .double(virtualTick)
            let decision = Self.decide(state: decisionState, changed: changed, now: tick, maxGap: options.maxGap)
            if decision.run { virtualTick = tick }
            observed = Observed(tickAt: tick, decision: decision, start: now, logSize: Files.size(stateDir + "/switch.log"), activeTs: activeReadAt())
            pendingDue = decisionState["tick_due"]
            startState = state
        }
        let status = Files.stamp(stateDir + "/status.json").map { "\($0)" } ?? "-"
        guard status != lastStatus else { return }
        lastStatus = status
        guard let o = observed else {
            // status.json from someone else (the menu bar's `status --json` doesn't write it; a manual tick may)
            return
        }
        // the tick ended (status.json is its last write): what it changed since it started
        observed = nil
        let after = stamps()
        var hard: [String] = []
        var soft: [String] = []
        let lines = Files.tail(stateDir + "/switch.log", from: o.logSize)
            .split(separator: "\n").map(String.init).filter { !$0.contains("  token: ") && !$0.isEmpty }
        if !lines.isEmpty { hard.append("log \(lines.count)") }
        for name in ["keychain", "pause.json"] where after[name] != o.start[name] { hard.append(name) }
        if activeReadAt() != o.activeTs { hard.append("active reading") }
        if after["usage-cache.json"] != o.start["usage-cache.json"] && activeReadAt() == o.activeTs { soft.append("other readings") }
        var stateNow = Self.comparable(state)
        var stateStart = Self.comparable(startState)
        // the hourly profile cache and the fingerprints are the panel's, not the switcher's
        for key in ["identity", "fingerprints"] {
            if stateNow[key] != stateStart[key] { soft.append(key) }
            stateNow[key] = nil
            stateStart[key] = nil
        }
        if stateNow != stateStart {
            hard.append("state: " + Set(stateNow.keys).union(stateStart.keys).filter { stateNow[$0] != stateStart[$0] }.sorted().joined(separator: ","))
        }
        let record = PyObject([
            ("at", .double(o.tickAt)), ("run", .bool(o.decision.run)), ("reasons", .array(o.decision.reasons.map { .string($0) })),
            ("hard", .array(hard.map { .string($0) })), ("soft", .array(soft.map { .string($0) })), ("due", pendingDue ?? .null),
        ])
        if let path = options.shadowPath { Files.append(path, PyJSON.object(record).dumps() + "\n") }
        if o.decision.run { runs += 1 } else { skips += 1 }
        baseline = after
        previousState = state
    }

    var pendingDue: PyJSON?
    var startState = PyObject()
    var lastStateStamp = ""
    var shadowState = PyObject()
    /// the last tick the shadow schedule would have run
    var virtualTick = 0.0

    /// tick_due as a claude-acc without it would have written it, from the files the tick left:
    /// quiet while the active account carries and nothing pauses, until its reading expires
    /// (active_max_age) or the Depot account's does (600 s) or Depot's token needs replacing.
    /// The active token's expiry isn't in any file, so this estimate can't know it.
    func estimatedDue(_ state: PyObject) -> PyObject? {
        let status = Files.json(stateDir + "/status.json", fallback: .null).object ?? PyObject()
        let cfg = Files.json(stateDir + "/config.json", fallback: .null).object ?? PyObject()
        let cache = Files.json(stateDir + "/usage-cache.json", fallback: .null).object ?? PyObject()
        if status["orca_selected"]?.truthy ?? false { return PyObject([("quiet", true), ("at", .null), ("why", "host selection")]) }
        if status["foreign_runtime"]?.truthy ?? false { return PyObject([("quiet", true), ("at", .null), ("why", "foreign runtime")]) }
        guard let email = status["active_email"]?.string,
              let hit = cache[email]?.object, let ts = hit["ts"]?.double, let data = hit["data"]?.object
        else { return PyObject([("quiet", false), ("at", .null), ("why", "")]) }
        let hardS = cfg["hard_session_left"]?.double ?? 5
        let hardW = cfg["hard_weekly_left"]?.double ?? 3
        let (session, weekly) = Self.left(data, now: Kernel.wall())
        // a pause ends quietly only with the headroom the switcher takes an account with (has_room)
        let minW = max(cfg["min_weekly_left"]?.double ?? 8, hardW + 1)
        let minS = max(cfg["min_session_left"]?.double ?? 15, hardS + 1)
        let roomy = weekly >= minW && session >= minS
        let paused = status["pause"]?.truthy ?? false
        guard session > hardS, weekly > hardW, roomy || !paused else { return PyObject([("quiet", false), ("at", .null), ("why", "")]) }
        let minutes = min((session - hardS) / 5, (weekly - hardW) / 1)
        var terms = [(ts + min(max(minutes * 60, 100), 900).rounded(.towardZero), "odczyt limitów aktywnego konta")]
        if cfg["depot_sync"]?.truthy ?? true, let depot = state["depot_email"]?.string, depot != email {
            if let dts = cache[depot]?["ts"]?.double { terms.append((dts + 600, "token albo konto sandboxów Depot")) }
            if let exp = state["depot_expires_at"]?.double, exp > 0 {
                terms.append((exp - (cfg["depot_min_valid_hours"]?.double ?? 4) * 3600, "token albo konto sandboxów Depot"))
            }
        }
        let first = terms.min { $0.0 < $1.0 }!
        return PyObject([("quiet", true), ("at", .double(first.0)), ("why", .string(first.1))])
    }

    /// headroom(settled(data)): what's left of the session and the week, a window past its reset empty
    static func left(_ data: PyObject, now: Double) -> (Double, Double) {
        func window(_ key: String) -> Double {
            let w = data[key]?.object ?? PyObject()
            if let reset = w["resets_at"]?.string, let at = ISODate.epoch(reset), at <= now { return 100 }
            return 100 - (w["utilization"]?.double ?? 0)
        }
        return (window("five_hour"), window("seven_day"))
    }

    /// When the active account was last read (usage-cache.json), by the email status.json names
    func activeReadAt() -> Double? {
        guard let email = Files.json(stateDir + "/status.json", fallback: .null)["active_email"]?.string else { return nil }
        return Files.json(stateDir + "/usage-cache.json", fallback: .null)[email]?["ts"]?.double
    }
}

extension HostResolver {
    public struct DataFiles: Sendable {
        public var userData: String
        public var dataFile: String
        /// the files orcahost.py's choice depends on (each host's SingletonLock, the Pod bundles)
        public var watch: [String]
    }

    /// orcahost.py's user_data, data_file and watch (a cold path: at start and when `watch` moves)
    public static func dataFiles(python: [String], home: String) -> DataFiles? {
        let script = GuardPaths.state(home) + "/orcahost.py"
        let argv = python.filter { !$0.hasSuffix("/acc.py") } + [script]
        guard let out = Spawn.output(argv, timeout: 20), let host = (try? PyJSON.loads(bytes: out))?.object,
              let userData = host["user_data"]?.string, let dataFile = host["data_file"]?.string
        else { return nil }
        return DataFiles(userData: userData, dataFile: dataFile, watch: host["watch"]?.array?.compactMap(\.string) ?? [])
    }

    /// The watched files as orcahost.py sees them: a link's target with whether the pid it names
    /// lives (SingletonLock is "<host>-<pid>"), else the path's stamp.
    public static func watchStamp(_ paths: [String]) -> String {
        paths.map { path -> String in
            var buffer = [CChar](repeating: 0, count: 1024)
            let n = readlink(path, &buffer, buffer.count - 1)
            guard n > 0 else { return path + "=" + (Files.stamp(path).map { "\($0)" } ?? "-") }
            let target = String(decoding: buffer[..<n].map { UInt8(bitPattern: $0) }, as: UTF8.self)
            let pid = target.split(separator: "-").last.flatMap { Int32($0) } ?? 0
            let alive = pid > 1 && (kill(pid, 0) == 0 || errno == EPERM)
            return path + "=" + target + (alive ? "+" : "-")
        }.joined(separator: "\n")
    }
}

extension Files {
    public static func size(_ path: String) -> Int64 {
        var st = stat()
        return stat(path, &st) == 0 ? st.st_size : 0
    }

    /// The file from byte `offset` on (all of it when it shrank: rotated)
    public static func tail(_ path: String, from offset: Int64) -> String {
        guard let data = read(path) else { return "" }
        let start = Int(offset) <= data.count ? Int(offset) : 0
        return String(decoding: data[start...], as: UTF8.self)
    }

    public static func append(_ path: String, _ text: String) {
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return }
        _ = Array(text.utf8).withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
    }
}

/// The API's ISO 8601 times ("2026-10-11T03:00:00.123456+00:00", "…Z") as epoch seconds.
enum ISODate {
    static func epoch(_ text: String) -> Double? {
        var t = tm()
        let base = String(text.unicodeScalars.prefix(19))
        guard base.count == 19, strptime(base, "%Y-%m-%dT%H:%M:%S", &t) != nil else { return nil }
        var seconds = Double(timegm(&t))
        var rest = Substring(text.unicodeScalars.dropFirst(19).map(String.init).joined())
        if rest.hasPrefix(".") {
            let digits = rest.dropFirst().prefix { $0.isASCII && $0.isNumber }
            seconds += Double("0." + digits) ?? 0
            rest = rest.dropFirst(1 + digits.count)
        }
        if rest == "Z" || rest.isEmpty { return seconds }
        let sign: Double = rest.hasPrefix("-") ? -1 : 1
        let offset = rest.dropFirst()
        let parts = offset.contains(":") ? offset.split(separator: ":") : offset.count == 4 ? [offset.prefix(2), offset.suffix(2)] : []
        guard parts.count == 2, let h = Double(parts[0]), let m = Double(parts[1]) else { return nil }
        return seconds - sign * (h * 3600 + m * 60)
    }
}
