// The scheduler's per-second work in acc-cored: one pass for every job instead of one per wrapper.
//
// A wrapper that knows acc-cored (sched.py, `cored`) marks its queue entry "wake": "usr1" and its
// running entry "monitor": "cored". For those acc-cored samples the job's process tree on the
// wrapper's cadence (10 ms, then 50 ms, then 250 ms), writes the heartbeat fields once a second,
// runs reap, refresh_memory, safety, plan and update_queue_view once for all, and wakes a waiting
// wrapper with SIGUSR1 when plan admits it (the wrapper then admits itself, as before). The
// wrapper blocks in wait4 meanwhile and asks for the job's final peak over the control socket.
// Wrappers that don't know acc-cored keep their own loops; while none of the jobs is ours,
// acc-cored only watches the directory. With no job left the timers stop.
import Darwin
import Dispatch

public final class SchedEngine: @unchecked Sendable {
    let stateDir: String
    var schedDir: String { stateDir + "/sched" }
    var statePath: String { schedDir + "/state.json" }
    var lockPath: String { schedDir + "/lock" }
    var configPath: String { ProcessEnv.get("SCHED_CONFIG") ?? schedDir + "/config.json" }
    // the wrappers it stands in for run at the default QoS: under a heavy load this must not starve
    let queue = DispatchQueue(label: "acc-cored.sched", qos: .userInitiated)
    var dirSource: DispatchSourceFileSystemObject?
    var tickTimer: DispatchSourceTimer?
    var sampleTimer: DispatchSourceTimer?
    var watches: [String: JobWatch] = [:]
    var ticking = false
    /// the guard's latest snapshot when the guard runs in this process
    public var snapshot: (@Sendable () -> PyObject?)?
    public private(set) var passes = 0
    public private(set) var wakes = 0

    public init(stateDir: String) { self.stateDir = stateDir }

    final class JobWatch {
        let id: String
        let pgid: pid_t
        let wrapper: pid_t
        let started: Double
        let native: Bool
        let simsSince: Double?
        var peak = 0.0
        var nowGB = 0.0
        var cpu = 0.0
        var own: Set<pid_t> = []
        var nextSample = 0.0
        var pool: (gb: Double, active: Bool, pids: Set<pid_t>)?
        var poolAt = 0.0
        var built = false
        // StallWatch
        var activeAt: Double?
        var mark: (cpu: Double, pids: Set<pid_t>, output: Int64)?
        var goneAt: Double?

        init(id: String, pgid: pid_t, wrapper: pid_t, started: Double, native: Bool, simsSince: Double?) {
            self.id = id
            self.pgid = pgid
            self.wrapper = wrapper
            self.started = started
            self.native = native
            self.simsSince = simsSince
        }

        /// one sample of run_local's loop: the job's tree (and the native pool), the peak
        func sample(now: Double) {
            own = SchedNative.jobPids(pgid)
            if native && now - poolAt >= 2.0 {
                pool = SchedNative.scan(simsSince: simsSince, builds: !built)
                poolAt = now
            }
            if let pool, !pool.pids.isEmpty {
                (nowGB, cpu) = SchedNative.usage(own.union(pool.pids))
            } else {
                (nowGB, cpu) = SchedNative.usage(own)
                nowGB += pool?.gb ?? 0
            }
            peak = max(peak, nowGB)
            let ran = now - started
            nextSample = now + (ran < 0.5 ? 0.01 : ran < 3 ? 0.05 : 0.25)
        }

        /// StallWatch.sample: seconds stalled (past stall_s) or nil while the job works
        func stalled(now: Double, stallS: Double) -> Double? {
            let output = SchedNative.outputSize(wrapper)
            if let m = mark, cpu - m.cpu < 0.5, m.pids == own, m.output == output {
                let idle = now - (activeAt ?? now)
                if stallS == 0 || idle < stallS { return nil }
                return idle
            }
            activeAt = now
            mark = (cpu, own, output)
            return nil
        }
    }

    /// Watches the sched directory: any state.json write may be a new job of ours.
    public func start() {
        Files.mkdirs(schedDir)
        let fd = open(schedDir, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete], queue: queue)
        source.setEventHandler { [weak self] in self?.kick() }
        source.setCancelHandler { close(fd) }
        source.resume()
        dirSource = source
        queue.async { self.pass() }
    }

    /// Stops the timers and takes the mark away, so new wrappers keep their own loops.
    public func stop() {
        queue.sync {
            dirSource?.cancel()
            tickTimer?.cancel()
            sampleTimer?.cancel()
            ticking = false
            let fd = open(lockPath, O_RDWR | O_CREAT | O_CLOEXEC, 0o644)
            guard fd >= 0 else { return }
            defer { close(fd) }
            guard GuardEngine.flockRetrying(fd, LOCK_EX) else { return }
            if var state = Files.json(statePath, fallback: .null).object, state["cored"]?["pid"] == .int(Int(getpid())) {
                state["cored"] = nil
                Self.saveState(statePath, &state, now: Kernel.wall())
            }
            _ = GuardEngine.flockRetrying(fd, LOCK_UN)
        }
    }

    func kick() {
        if !ticking { pass() }
    }

    /// One pass: under sched/lock, the heartbeats of our jobs, then the housekeeping, the wakes and
    /// the save; with nothing of ours left the timers stop until the next write in the directory.
    func pass() {
        let fd = open(lockPath, O_RDWR | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return }
        guard GuardEngine.flockRetrying(fd, LOCK_EX) else {
            close(fd)
            return
        }
        defer {
            _ = GuardEngine.flockRetrying(fd, LOCK_UN)
            close(fd)
        }
        let cfg = SchedConfig.load(path: configPath)
        let probes = LiveSchedProbes(stateDir: stateDir, snapshot: snapshot?())
        let now = probes.now
        var state = Self.loadState(statePath, cfg, now: now, day: probes.day())
        let running = (state["running"]?.array ?? []).compactMap(\.object)
        let queued = (state["queue"]?.array ?? []).compactMap(\.object)
        let ours = running.contains { $0["monitor"] == .string("cored") } || queued.contains { $0["wake"] == .string("usr1") }
        updateWatches(running, now: now)
        guard ours else {
            // the mark new wrappers look for (sched.cored_pid): written once, not every pass
            if state["cored"]?["pid"] != .int(Int(getpid())) {
                state["cored"] = .object(PyObject([("pid", .int(Int(getpid()))), ("at", .double(now))]))
                Self.saveState(statePath, &state, now: now)
            }
            idle()
            return
        }
        passes += 1
        var updated = running
        for i in updated.indices where updated[i]["monitor"] == .string("cored") {
            guard let id = updated[i]["id"]?.string, let w = watches[id] else { continue }
            heartbeat(&updated[i], w, cfg: cfg, state: &state, now: now)
        }
        state["running"] = .array(updated.map { .object($0) })
        let result = Sched.tick(&state, cfg, probes)
        let admitted = Set(result.admitted.map(\.0))
        for job in (state["queue"]?.array ?? []).compactMap(\.object) where job["wake"] == .string("usr1") {
            let due = admitted.contains(job["id"] ?? .null) || (job["cancelled"]?.truthy ?? false)
            if due, let pid = job["pid"]?.int, kill(pid_t(clamping: pid), SIGUSR1) == 0 { wakes += 1 }
        }
        state["cored"] = .object(PyObject([("pid", .int(Int(getpid()))), ("at", .double(now))]))
        Self.saveState(statePath, &state, now: now)
        if !ticking {
            ticking = true
            let t = DispatchSource.makeTimerSource(queue: queue)
            t.schedule(deadline: .now() + 1, repeating: 1, leeway: .milliseconds(100))
            t.setEventHandler { [weak self] in self?.pass() }
            t.resume()
            tickTimer = t
        }
        scheduleSamples()
    }

    func idle() {
        tickTimer?.cancel()
        tickTimer = nil
        ticking = false
        if watches.isEmpty {
            sampleTimer?.cancel()
            sampleTimer = nil
        }
    }

    /// One watch per running job we monitor; a watch outlives its entry for 30 s (the final peak)
    func updateWatches(_ running: [PyObject], now: Double) {
        var seen: Set<String> = []
        for job in running where job["monitor"] == .string("cored") {
            guard let id = job["id"]?.string, let pgid = job["child_pgid"]?.int, let wrapper = job["pid"]?.int else { continue }
            seen.insert(id)
            if watches[id] == nil {
                let started = job["child_started_at"]?.double ?? job["started_at"]?.double ?? now
                let native = job["lang"] == .string("native") && (job["exclusive"]?.truthy ?? false)
                let w = JobWatch(
                    id: id, pgid: pid_t(clamping: pgid), wrapper: pid_t(clamping: wrapper), started: started, native: native,
                    simsSince: job["tool"] == .string("portivo-mobile") ? started : nil)
                w.sample(now: now)
                watches[id] = w
            }
        }
        for (id, w) in watches where !seen.contains(id) {
            if w.goneAt == nil { w.goneAt = now }
            if now - w.goneAt! > 30 { watches[id] = nil }
        }
    }

    /// heartbeat(): the running entry's fields, from the watch's samples
    func heartbeat(_ me: inout PyObject, _ w: JobWatch, cfg: SchedConfig, state: inout PyObject, now: Double) {
        if w.native, let pool = w.pool, me["exclusive"]?.truthy ?? false {
            Self.trackNative(&me, active: pool.active, now: now, internalState: state["_internal"]?.object ?? PyObject(), nowGB: w.nowGB)
        }
        let stalled = w.stalled(now: now, stallS: cfg.num("stall_s").value)
        let elapsed = now - w.started
        let wall = PyNum.or(me["predicted_wall_s"], .int(60))
        me["elapsed_s"] = pyRound(.double(elapsed), 1).json
        me["mem_now_gb"] = pyRound(.double(w.nowGB), 2).json
        me["mem_peak_gb"] = pyRound(.double(w.peak), 2).json
        me["cpu_cores"] = elapsed > 1 ? pyRound(.double(w.cpu / elapsed), 1).json : .null
        var eta = me
        eta["predicted_wall_s"] = wall.json
        eta["started_at"] = .double(w.started)
        me["eta_s"] = pyRound(Sched.timeLeft(eta, now), 1).json
        me["progress"] = pyRound(pyMin(.double(0.99), .double(elapsed) / wall), 2).json
        me["stalled_s"] = stalled.flatMap { $0 != 0 ? PyJSON.int(GuardText.round($0)) : nil } ?? .null
        if me["native_done"]?.truthy ?? false { w.built = true }
    }

    /// track_native(me, native, now, internal, now_gb)
    static func trackNative(_ me: inout PyObject, active: Bool, now: Double, internalState: PyObject, nowGB: Double) {
        if me["native_done"]?.truthy ?? false { return }
        if active {
            me["native_seen"] = true
            me["native_active_at"] = .double(now)
            me["mem_predicted_gb"] = pyMax(PyNum.or(me["mem_predicted_gb"], .double(0)), Sched.nativeBuildGB(internalState)).json
            return
        }
        let seen = me["native_seen"]?.truthy ?? false
        let quiet = seen && now - (me["native_active_at"]?.double ?? now) >= 30
        let floor = Self.nativeFloor[me["native_tool"]?.string ?? ""] ?? 0
        let stale = !seen && now - (me["started_at"]?.double ?? now) > 2 * max(PyNum.or(me["predicted_wall_s"], .int(0)).value, floor, 300)
        if quiet || stale {
            me["native_done"] = true
            me["native_done_at"] = .double(now)
            me["mem_predicted_gb"] = pyRound(.double(nowGB), 2).json
        }
    }

    /// NATIVE[tool][2]: the table's wall time per native tool
    static let nativeFloor: [String: Double] = [
        "xcodebuild": 600, "expo-run": 900, "react-native-run": 900, "eas-local": 1500, "gradle": 600, "portivo-mobile": 900,
        "pod": 180, "expo-prebuild": 180, "simulator": 30,
    ]

    /// Sampling on the earliest due watch; with no watch the timer stops.
    func scheduleSamples() {
        sampleTimer?.cancel()
        sampleTimer = nil
        let live = watches.values.filter { $0.goneAt == nil }
        guard let next = live.map(\.nextSample).min() else { return }
        let t = DispatchSource.makeTimerSource(queue: queue)
        let delay = max(0.005, next - Kernel.wall())
        t.schedule(deadline: .now() + delay, leeway: .milliseconds(delay < 0.1 ? 2 : 20))
        t.setEventHandler { [weak self] in
            guard let self else { return }
            let now = Kernel.wall()
            for w in self.watches.values where w.goneAt == nil && w.nextSample <= now + 0.002 { w.sample(now: now) }
            self.scheduleSamples()
        }
        t.resume()
        sampleTimer = t
    }

    /// The final numbers of a job for its wrapper (`sched.final`): peak and live CPU of the last sample.
    public func final(_ id: String) -> PyJSON {
        queue.sync {
            guard let w = watches[id] else { return .object(PyObject([("ok", false)])) }
            if w.goneAt == nil { w.sample(now: Kernel.wall()) }
            return .object(PyObject([("ok", true), ("peak_gb", .double(w.peak)), ("cpu_s", .double(w.cpu)), ("now_gb", .double(w.nowGB))]))
        }
    }

    // MARK: state.json

    /// load_state(cfg)
    static func loadState(_ path: String, _ cfg: SchedConfig, now: Double, day: String) -> PyObject {
        var state = Files.json(path, fallback: .null).object ?? PyObject()
        if state["version"] != .int(1) { state = emptyState(cfg, now: now, day: day) }
        var internalState = state["_internal"]?.object ?? PyObject([("swap", .array([])), ("avail", .array([]))])
        if state["today"]?["date"] != .string(day) {
            state["today"] = newToday(day)
            internalState["old_lock"] = newOldLock()
        }
        if internalState["old_lock"] == nil {
            internalState["vlock_free_at"] = nil
            internalState["old_lock"] = newOldLock()
            var today = state["today"]?.object ?? PyObject()
            today["old_lock_wait_s"] = 0.0
            today["go_wait_s"] = 0.0
            today["wait_saved_s"] = 0.0
            state["today"] = .object(today)
        }
        state["_internal"] = .object(internalState)
        state["config"] = .object(PyObject(["headroom_gb", "lambda_s_per_unit", "unit_usd", "small_gb", "small_wall_s"].map { ($0, cfg[$0]) }))
        return state
    }

    static func newToday(_ day: String) -> PyJSON {
        .object(PyObject([
            ("date", .string(day)), ("jobs_local", 0), ("jobs_depot", 0), ("wait_s", 0.0), ("old_lock_wait_s", 0.0), ("go_wait_s", 0.0),
            ("wait_saved_s", 0.0), ("depot_units", 0.0), ("depot_cost_usd", 0.0), ("local_kept_usd", 0.0), ("overtakes", 0), ("pauses", 0),
            ("peak_concurrency", 0), ("max_reserved_gb", 0.0),
        ]))
    }

    static func newOldLock() -> PyJSON { .object(PyObject([("wait_s", 0.0), ("free_at", 0.0), ("pending", .array([]))])) }

    static func emptyState(_ cfg: SchedConfig, now: Double, day: String) -> PyObject {
        let ram = Double(Kernel.int("hw.memsize") ?? 0) / Sched.GB
        return PyObject([
            ("version", 1), ("updated_at", .double(now)), ("idle_since", .double(now)),
            ("host", .object(PyObject([
                ("ram_gb", pyRound(.double(ram), 1).json), ("cores_p", .int(Int(Kernel.int("hw.perflevel0.physicalcpu") ?? 0))),
                ("cores_e", .int(Int(Kernel.int("hw.perflevel1.physicalcpu") ?? 0))),
            ]))),
            ("config", .object(PyObject())), ("memory", .object(PyObject())), ("running", .array([])), ("queue", .array([])),
            ("overtakes", .array([])), ("recent", .array([])), ("today", newToday(day)),
            ("_internal", .object(PyObject([("swap", .array([])), ("avail", .array([])), ("old_lock", newOldLock())]))),
        ])
    }

    /// save_state(state): updated_at, idle_since, then the file by rename (indent 1, not ASCII-only)
    static func saveState(_ path: String, _ state: inout PyObject, now: Double) {
        state["updated_at"] = .double(now)
        let busy = (state["running"]?.array?.isEmpty == false) || (state["queue"]?.array?.isEmpty == false)
        if busy {
            state["idle_since"] = .null
        } else if !(state["idle_since"]?.truthy ?? false) {
            state["idle_since"] = .double(now)
        }
        _ = Files.writeAtomic(path, PyJSON.object(state).dumps(indent: 1, ensureASCII: false), tmpSuffix: ".tmp\(getpid())")
    }
}

/// getenv without Foundation
public enum ProcessEnv {
    public static func get(_ name: String) -> String? {
        guard let p = getenv(name) else { return nil }
        let s = String(cString: p)
        return s.isEmpty ? nil : s
    }
}
