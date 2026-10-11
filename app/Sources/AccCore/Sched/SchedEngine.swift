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
//
// The mark ("cored": pid, at, proto) is what a wrapper trusts: it pings the socket before it
// attaches, and while attached it treats a mark older than CORED_STALE_S as acc-cored gone (sched.py).
// So a pass that can't run (sched/lock held by Python: retried in 100 ms, sampling goes on) or
// mustn't (a state of another version or unreadable, a config the native pass reads otherwise:
// nothing written) lets the wrappers fall back on their own within seconds.
import Darwin
import Dispatch
import Synchronization

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
    /// `wakes` for ping, which doesn't wait for this engine's queue
    public let wakeCount = Atomic<Int>(0)
    /// sched.py's CORED_PROTO: what the mark and the socket mean; a wrapper of another one ignores us
    public static let proto = 1
    var retryPending = false

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

    /// A write in the directory: a pass when idle; while ticking, the watches only (a new job's first
    /// sample must not wait for the next pass, or a short job gets none). state.json is replaced
    /// atomically, so it reads without the lock.
    func kick() {
        if !ticking {
            pass()
            return
        }
        guard let state = Files.json(statePath, fallback: .null).object, state["version"] == .int(1) else { return }
        updateWatches((state["running"]?.array ?? []).compactMap(\.object), now: Kernel.wall())
        scheduleSamples()
    }

    /// One pass: under sched/lock, the heartbeats of our jobs, then the housekeeping, the wakes and
    /// the save; with nothing of ours left the timers stop until the next write in the directory.
    func pass() {
        let fd = open(lockPath, O_RDWR | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return }
        // Python's heartbeat took the lock without waiting; a pass that would wait stops the
        // samples too (same queue), so it tries again in 100 ms instead
        guard GuardEngine.flockRetrying(fd, LOCK_EX | LOCK_NB) else {
            close(fd)
            if !retryPending {
                retryPending = true
                queue.asyncAfter(deadline: .now() + 0.1) { [weak self] in
                    self?.retryPending = false
                    self?.pass()
                }
            }
            return
        }
        defer {
            _ = GuardEngine.flockRetrying(fd, LOCK_UN)
            close(fd)
        }
        let cfg = SchedConfig.load(path: configPath)
        let probes = LiveSchedProbes(stateDir: stateDir, snapshot: snapshot?())
        let now = probes.now
        guard var state = Self.loadState(statePath, cfg, now: now, day: probes.day()) else {
            standBack()  // a state of another version, or unreadable: Python's to handle; nothing written
            return
        }
        if !cfg.mismatched.isEmpty {
            // a config the native pass would read otherwise: the wrappers keep their own loops
            if state["cored"] != nil {
                state["cored"] = nil
                Self.saveState(statePath, &state, now: now)
            }
            standBack()
            return
        }
        let running = (state["running"]?.array ?? []).compactMap(\.object)
        let queued = (state["queue"]?.array ?? []).compactMap(\.object)
        let ours = running.contains { $0["monitor"] == .string("cored") } || queued.contains { $0["wake"] == .string("usr1") }
        updateWatches(running, now: now)
        guard ours else {
            // the mark new wrappers look for (sched.cored_pid): written once, not every pass
            if state["cored"]?["pid"] != .int(Int(getpid())) || state["cored"]?["proto"] != .int(Self.proto) {
                state["cored"] = mark(now)
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
        let waiting = (state["queue"]?.array ?? []).compactMap(\.object)
        wakes += Self.wake(waiting, admitted: Set(result.admitted.map(\.0))) { kill($0, SIGUSR1) == 0 }
        wakeCount.store(wakes, ordering: .relaxed)
        state["cored"] = mark(now)
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

    /// SIGUSR1 to the queued wrappers that wait for it (`wake: usr1`) and are due (admitted or
    /// cancelled), with signal_job's checks: a pid above 1 that is still a scheduler wrapper (pids
    /// get reused). The count of signals sent.
    static func wake(_ queued: [PyObject], admitted: Set<PyJSON>, isWrapper: (pid_t) -> Bool = SchedNative.isWrapper,
                     signal: (pid_t) -> Bool) -> Int {
        var sent = 0
        for job in queued where job["wake"] == .string("usr1") {
            let due = admitted.contains(job["id"] ?? .null) || (job["cancelled"]?.truthy ?? false)
            guard due, let p = job["pid"]?.int, p > 1, p <= Int(Int32.max) else { continue }
            let pid = pid_t(p)
            if isWrapper(pid), signal(pid) { sent += 1 }
        }
        return sent
    }

    /// The pass stands back (a state of another version, a config read otherwise): the wrappers
    /// watch their own jobs again, so these watches go, and with them the samples.
    func standBack() {
        watches.removeAll()
        idle()
    }

    func mark(_ now: Double) -> PyJSON {
        .object(PyObject([("pid", .int(Int(getpid()))), ("at", .double(now)), ("proto", .int(Self.proto))]))
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

    /// One watch per running job we monitor; a watch outlives its entry for 30 s (the final peak).
    /// Only on a group that is still the job's: an entry from before a reboot, or one whose group
    /// was reused, would sample someone else's processes until reap drops it.
    func updateWatches(_ running: [PyObject], now: Double, start: (pid_t) -> Double? = SchedNative.startTime,
                       boot: Double = SchedNative.bootTime()) {
        var seen: Set<String> = []
        for job in running where job["monitor"] == .string("cored") {
            guard let id = job["id"]?.string, let pgid = job["child_pgid"]?.int, pgid > 1, let wrapper = job["pid"]?.int, wrapper > 1
            else { continue }
            if watches[id] == nil, Sched.stale(.object(job), "started_at", boot) || Sched.groupOf(.object(job), start: start) == nil {
                continue
            }
            seen.insert(id)
            if watches[id] == nil {
                let started = job["child_started_at"]?.double ?? job["started_at"]?.double ?? now
                let native = job["lang"] == .string("native") && (job["exclusive"]?.truthy ?? false)
                let w = JobWatch(
                    id: id, pgid: pid_t(clamping: pgid), wrapper: pid_t(clamping: wrapper), started: started, native: native,
                    simsSince: job["native_tool"] == .string("portivo-mobile") ? started : nil)
                // a restart of acc-cored mid-job: the peak so far is in the entry
                w.peak = job["mem_peak_gb"]?.double ?? 0
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
            // the entry's peak too: an earlier instance (or the wrapper's own loop) may have seen more
            let entry = (Files.json(statePath, fallback: .null)["running"]?.array ?? []).first { $0["id"]?.string == id }
            let peak = max(w.peak, entry?["mem_peak_gb"]?.double ?? 0)
            return .object(PyObject([("ok", true), ("peak_gb", .double(peak)), ("cpu_s", .double(w.cpu)), ("now_gb", .double(w.nowGB))]))
        }
    }

    // MARK: state.json

    /// load_state(cfg), except that a state acc-cored can't take as is (unreadable, or another
    /// version: sched.py may be newer than this daemon) is nil rather than a fresh one: writing a
    /// fresh one would wipe every queued and running job
    static func loadState(_ path: String, _ cfg: SchedConfig, now: Double, day: String) -> PyObject? {
        var state: PyObject
        if Files.exists(path) {
            guard let read = Files.json(path, fallback: .null).object, read["version"] == .int(1) else { return nil }
            state = read
        } else {
            state = emptyState(cfg, now: now, day: day)
        }
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
