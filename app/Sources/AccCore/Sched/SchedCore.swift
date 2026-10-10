// The memory scheduler's housekeeping, ported from sched.py line by line: reap, the reservations
// (reserve_target, growth_left, time_left), refresh_memory, safety, plan (with backfill and
// shadow) and update_queue_view. Today every waiting wrapper runs them twice a second and every
// running one once a second, each with its own state.json load and save; acc-cored runs them once
// for all. tests/acc_cored/sched_replay.py holds the Python side of every comparison.
import Darwin

/// What sched.py reads besides the state: the clock, memory, the guard's snapshot, processes.
public protocol SchedProbes: AnyObject {
    /// time.time()
    var now: Double { get }
    /// probe_memory(): level, ram_gb, swap_gb, pressure (and "native" in tests)
    func memory() -> PyObject
    /// devguard_snapshot()
    func guardSnapshot() -> PyObject
    /// devguard.json's max_server_gb as devserver_reserve_gb reads it (nil: unreadable)
    func guardMaxServerGB() -> PyJSON?
    /// native_scan(): {gb, active, pids}
    func nativeScan() -> (gb: Double, active: Bool, pids: Set<pid_t>)
    /// alive(pid)
    func alive(_ pid: PyJSON?) -> Bool
    /// descendants(pid) through proc_listpids(PROC_PPID_ONLY)
    func descendants(_ pid: pid_t) -> Set<pid_t>
    /// os.killpg, errors ignored
    func killpg(_ pgid: pid_t, _ signal: Int32) -> Bool
    /// time.strftime("%Y-%m-%d") at `now`
    func day() -> String
}

public struct SchedConfig: Sendable {
    public var raw: PyObject

    public static let defaults = PyObject([
        ("headroom_gb", 4.0), ("lambda_s_per_unit", 6.0), ("unit_usd", 0.006), ("small_gb", 4.5), ("small_wall_s", 120),
        ("starve_s", 120), ("aging_s", 300), ("stall_s", 600), ("class_timeout_s", .object(PyObject())), ("job_qos", .null),
        ("head_delay_s", 60), ("drop_count1", true), ("pause_swap_gb", 0.5), ("ldflags_w_for_build", true),
        ("depot_eta_since", "2026-10-05"), ("count1_trusted_exec", ["internal/testhelpers/testpg"]), ("idle_floor_pct", 65),
        ("depot_org", ""), ("node", true), ("native", true),
    ])

    public init(raw: PyObject) { self.raw = raw }

    /// load_config(): the defaults with the file's keys over them (a dict only)
    public static func load(path: String) -> SchedConfig {
        var cfg = defaults
        if let file = Files.json(path, fallback: .null).object {
            for (k, v) in file { cfg[k] = v }
        }
        return SchedConfig(raw: cfg)
    }

    public subscript(key: String) -> PyJSON { raw[key] ?? .null }
    public func num(_ key: String) -> PyNum { PyNum(raw[key]) ?? .int(0) }
}

public enum Sched {
    public static let GB = 1_073_741_824.0
    public static let stageText = ["normal", "tight", "brake", "emergency"]
    public static let longLivedFamilies = ["dev", "metro", "watchers", "simulators", "headless", "lsp", "docker"]
    public static let guardFreshS = 60.0
    /// NATIVE["xcodebuild"][1]
    public static let xcodebuildGB = 10.0
    static let reserveWarmup = (60.0, 0.5, 300.0)
    static let reserveGrowth = (1.5, 1.0)
    static let backfillSlack = (2.0, 10.0)

    // MARK: jobs

    static func f(_ job: PyObject, _ key: String) -> PyJSON? { job[key] }
    static func n(_ job: PyObject, _ key: String) -> PyNum? { PyNum(job[key]) }

    /// reap(state): entries of dead wrappers go; their orphaned job gets SIGCONT, SIGTERM
    public static func reap(_ state: inout PyObject, _ probes: SchedProbes) {
        var keep: [PyJSON] = []
        for job in state["running"]?.array ?? [] {
            if probes.alive(job["pid"]) {
                keep.append(job)
                continue
            }
            if let pgid = job["child_pgid"], pgid.truthy, probes.alive(pgid), let g = pgid.int {
                for sig in [SIGCONT, SIGTERM] { _ = probes.killpg(pid_t(clamping: g), sig) }
            }
        }
        state["running"] = .array(keep)
        state["queue"] = .array((state["queue"]?.array ?? []).filter { probes.alive($0["pid"]) })
    }

    /// reserve_target(job, now)
    static func reserveTarget(_ job: PyObject, _ now: Double) -> PyNum {
        let predicted = PyNum.or(job["mem_predicted_gb"], .double(0))
        if job["lang"] == .string("native") || (job["outside"]?.truthy ?? false) { return predicted }
        let elapsed = now - PyNum.or(job["started_at"], .double(now)).value
        let (lo, share, hi) = reserveWarmup
        let wall = PyNum.or(job["predicted_wall_s"], .int(0)).value
        if elapsed < min(hi, max(lo, share * wall)) { return predicted }
        let seen = pyMax(PyNum.or(job["mem_peak_gb"], .double(0)), PyNum.or(job["mem_now_gb"], .double(0)))
        return pyMin(predicted, seen * .double(reserveGrowth.0) + .double(reserveGrowth.1))
    }

    /// growth_left(job, now)
    static func growthLeft(_ job: PyObject, _ now: Double) -> PyNum {
        if (job["paused"]?.truthy ?? false) || (job["stalled_s"]?.truthy ?? false) { return .double(0) }
        let elapsed = now - PyNum.or(job["started_at"], .double(now)).value
        if elapsed > max(600.0, 3 * PyNum.or(job["predicted_wall_s"], .int(0)).value) { return .double(0) }
        return pyMax(.double(0), reserveTarget(job, now) - PyNum.or(job["mem_now_gb"], .int(0)))
    }

    /// time_left(job, now)
    static func timeLeft(_ job: PyObject, _ now: Double) -> PyNum {
        let wall = PyNum.or(job["predicted_wall_s"], .int(60))
        let elapsed = now - PyNum.or(job["started_at"], .double(now)).value
        let left = wall - .double(elapsed)
        return left.value >= 0 ? pyMax(left, .double(5)) : pyMax(.double(0.5 * elapsed), .double(5))
    }

    static func local(_ state: PyObject) -> [PyObject] {
        (state["running"]?.array ?? []).compactMap(\.object).filter { $0["where"] == .string("local") }
    }

    // MARK: the guard's snapshot

    static func at(_ snap: PyObject) -> Double? {
        switch snap["at"] {
        case nil: 0
        case .int(let i)?: Double(i)
        case .double(let d)?: d
        case .string(let s)?: Double(s.trimmingPySpace())
        case .bool(let b)?: b ? 1 : 0
        default: nil
        }
    }

    /// guard_level(snap)
    static func guardLevel(_ snap: PyObject, now: Double) -> Int? {
        guard let a = at(snap), now - a < guardFreshS else { return nil }
        switch snap["pressure"]?["level"] {
        case .int(let i)?: return i
        case .double(let d)? where d.isFinite: return GuardText.int(d)
        case .bool(let b)?: return b ? 1 : 0
        default: return nil
        }
    }

    /// devserver_reserve_gb(snap)
    static func devserverReserveGB(_ snap: PyObject, _ probes: SchedProbes, now: Double) -> PyNum {
        var maxServer = PyNum.double(4.0)
        if let v = probes.guardMaxServerGB(), let d = v.double { maxServer = .double(d) }
        guard let a = at(snap), now - a < 120 else { return maxServer }
        let room = PyNum.double(((snap["budget"]?.double ?? 0) - (snap["total"]?.double ?? 0)) / GB)
        var regrows: [PyNum] = []
        for u in snap["units"]?.array ?? [] where u["protected"]?.truthy ?? false {
            let r = PyNum.double(max(0.0, PyNum.or(u["regrow"], .int(0)).value) / GB)
            regrows.append(pyMin(r, maxServer))
        }
        let regrow = pySum(regrows)
        return pyRound(pyMax(pyMax(.double(0), pyMin(maxServer, room)), regrow), 2)
    }

    /// simulators_info(snap)
    static func simulatorsInfo(_ snap: PyObject, now: Double) -> PyJSON {
        guard let a = at(snap), now - a < 120, snap["simulators"] != nil else { return .null }
        let sims = (snap["simulators"]?.array ?? []).compactMap(\.object)
        let used = sims.filter { $0["in_use"]?.truthy ?? false }
        let agents = used.filter { ($0["pool"]?.truthy ?? false) && !($0["protected"]?.truthy ?? false) }
        let gb = pySum(sims.map { PyNum.double(PyNum.or($0["footprint"], .int(0)).value) })
        let holders: [PyJSON] = agents.prefix(4).map { s in
            let name = s["name"] ?? .null
            return name.truthy ? name : (s["udid"] ?? .null)
        }
        return .object(PyObject([
            ("booted", .int(sims.count)), ("in_use", .int(used.count)), ("agents_in_use", .int(agents.count)),
            ("cap", .int(GuardText.int(PyNum.or(snap["simulator_cap"], .int(0)).value))),
            ("gb", pyRound(pySum([gb]) / .double(GB), 2).json), ("holders", .array(Array(holders))),
        ]))
    }

    /// guard_view(snap): (stage, long_lived GB, families)
    static func guardView(_ snap: PyObject, now: Double) -> (Int, PyJSON, PyJSON) {
        if snap.isEmpty { return (0, .null, .array([])) }
        guard let a = at(snap) else { return (0, .null, .array([])) }
        let age = now - a
        if age >= 120 { return (0, .null, .array([])) }
        let stage = age < 30 ? (PyNum.or(snap["pressure"]?["stage"], .int(0)).value).rounded(.towardZero) : 0
        let inv = snap["inventory"]?.object ?? PyObject()
        let families = inv["families"]?.object ?? PyObject()
        var fams: [(PyNum, PyJSON)] = []
        for (name, f) in families where longLivedFamilies.contains(name) {
            let fp = PyNum(f["footprint"]) ?? .int(0)
            fams.append((fp, .object(PyObject([
                ("family", .string(name)), ("gb", pyRound(fp / .double(GB), 2).json), ("count", f["count"] ?? .null),
            ]))))
        }
        // sorted(items, key=-footprint): stable, in the dict's order among equals
        let ordered = fams.enumerated().sorted { l, r in
            l.element.0.value != r.element.0.value ? l.element.0.value > r.element.0.value : l.offset < r.offset
        }.map(\.element.1)
        let longLived = inv["long_lived"].flatMap { v -> PyJSON? in
            guard let n = PyNum(v) else { return v.isNull ? nil : .null }
            return pyRound(n / .double(GB), 2).json
        } ?? .null
        return (GuardText.int(stage), longLived, .array(ordered))
    }

    // MARK: native builds

    /// native_owner(state)
    static func nativeOwner(_ state: PyObject) -> PyObject? {
        let running = (state["running"]?.array ?? []).compactMap(\.object)
        if let own = running.first(where: {
            $0["where"] == .string("local") && ($0["exclusive"]?.truthy ?? false) && !($0["native_done"]?.truthy ?? false)
        }) { return own }
        let host = state["_internal"]?["native"]?["in_job"]
        guard let host, host.truthy else { return nil }
        return running.first { $0["id"] == host }
    }

    /// native_host(local, pids)
    static func nativeHost(_ local: [PyObject], _ pids: Set<pid_t>, _ probes: SchedProbes) -> PyJSON {
        for j in local {
            guard let pid = j["pid"], pid.truthy, !(j["exclusive"]?.truthy ?? false), let p = pid.int else { continue }
            if !probes.descendants(pid_t(clamping: p)).isDisjoint(with: pids) { return j["id"] ?? .null }
        }
        return .null
    }

    /// native_build_gb(internal)
    static func nativeBuildGB(_ internalState: PyObject) -> PyNum {
        let peaks = (internalState["native_peaks"]?.array ?? []).compactMap { PyNum($0) }.filter(\.truthy)
        if peaks.isEmpty { return .double(xcodebuildGB) }
        var gb = percentile(peaks) * .double(1.15)
        if peaks.count < 3 { gb = pyMax(gb, .double(xcodebuildGB * 0.8)) }
        return pyRound(gb, 2)
    }

    /// percentile(values, 0.9)
    static func percentile(_ values: [PyNum], _ q: Double = 0.9) -> PyNum {
        guard !values.isEmpty else { return .int(0) }  // Python raises; the caller never asks
        let v = values.enumerated().sorted { l, r in l.element.value != r.element.value ? l.element.value < r.element.value : l.offset < r.offset }.map(\.element)
        let pos = Double(v.count - 1) * q
        let lo = Int(pos)
        let hi = min(lo + 1, v.count - 1)
        return v[lo] + (v[hi] - v[lo]) * .double(pos - Double(lo))
    }

    /// sim_wait(job, mem)
    static func simWait(_ job: PyObject, _ mem: PyObject) -> Bool {
        let sims = (mem["simulators"]?.truthy ?? false) ? mem["simulators"]!.object ?? PyObject() : PyObject()
        let tool = job["native_tool"]
        guard tool == .string("portivo-mobile") || tool == .string("simulator") else { return false }
        guard !(job["sim_lease"]?.truthy ?? false), sims["cap"]?.truthy ?? false else { return false }
        return (PyNum(sims["agents_in_use"]) ?? .int(0)) >= (PyNum(sims["cap"]) ?? .int(0))
    }

    // MARK: refresh_memory

    /// refresh_memory(state, cfg): the memory view the queue and the panel read
    @discardableResult
    public static func refreshMemory(_ state: inout PyObject, _ cfg: SchedConfig, _ probes: SchedProbes) -> PyObject {
        let mem = probes.memory()
        let now = probes.now
        let ram = PyNum(mem["ram_gb"]) ?? .double(48)
        let available = (PyNum(mem["level"]) ?? .double(50)) / .int(100) * ram
        let local = local(state)
        let jobsNow = pySum(local.map { PyNum.or($0["mem_now_gb"], .int(0)) })
        var reserved = pySum(local.map { growthLeft($0, now) })
        let snap = probes.guardSnapshot()
        let dev = devserverReserveGB(snap, probes, now: now)
        let (stage, longLived, families) = guardView(snap, now: now)
        var internalState = state["_internal"]?.object ?? PyObject()
        var nat = internalState["native"]?.object ?? PyObject()
        if let fake = mem["native"] {
            let f = fake.object ?? PyObject()
            nat = PyObject([("at", .double(now)), ("gb", .double(PyNum.or(f["gb"], .int(0)).value)), ("active", .bool(f["active"]?.truthy ?? false))])
        } else if now - PyNum.or(nat["at"], .int(0)).value >= 5 {
            let scan = probes.nativeScan()
            nat = PyObject([("at", .double(now)), ("gb", pyRound(.double(scan.gb), 2).json), ("active", .bool(scan.active))])
            if scan.active { nat["in_job"] = nativeHost(local, scan.pids, probes) }
        }
        internalState["native"] = .object(nat)
        state["_internal"] = .object(internalState)
        let owner = nativeOwner(state)
        let outside = (nat["active"]?.truthy ?? false) && owner == nil
        let natGB = PyNum(nat["gb"]) ?? .int(0)
        let nativeReserve = outside ? pyMax(.double(0), nativeBuildGB(internalState) - natGB) : .double(0)
        reserved = reserved + nativeReserve
        let swapGB = PyNum(mem["swap_gb"]) ?? .double(0)
        var swap = (internalState["swap"]?.array ?? []).filter { now - ($0.array?.first?.double ?? 0) <= 120 }
        swap.append(.array([.double(now), swapGB.json]))
        internalState["swap"] = .array(Array(swap.suffix(240)))
        let day = probes.day()
        var avail = (internalState["avail"]?.array ?? []).filter { a in
            guard let x = a.array, x.count > 2 else { return false }
            return now - (x[2].double ?? 0) < 7 * 86400
        }
        if local.isEmpty {
            if var last = avail.last?.array, last.first == .string(day) {
                last[1] = pyMax(PyNum(last[1]) ?? .int(0), available).json
                last[2] = .double(now)
                avail[avail.count - 1] = .array(last)
            } else {
                avail.append(.array([.string(day), available.json, .double(now)]))
            }
        }
        internalState["avail"] = .array(Array(avail.suffix(8)))
        state["_internal"] = .object(internalState)
        // max([a[1] for a in avail] + [floor]) then min(…, 0.85 * ram)
        var top: PyNum = cfg.num("idle_floor_pct") / .int(100) * ram
        var candidates = avail.compactMap { $0.array.flatMap { PyNum($0[1]) } }
        candidates.append(top)
        top = candidates.dropFirst().reduce(candidates[0]) { pyMax($0, $1) }
        let idleTop = pyMin(top, .double(0.85) * ram)
        var host = state["host"]?.object ?? PyObject()
        host["ram_gb"] = pyRound(ram, 1).json
        state["host"] = .object(host)
        let headroom = cfg.num("headroom_gb")
        let natTool = nativeOwnerInfo(owner)
        var memory = PyObject()
        memory["level_pct"] = .int(GuardText.round((PyNum(mem["level"]) ?? .double(50)).value))
        memory["available_gb"] = pyRound(available, 2).json
        memory["headroom_gb"] = cfg["headroom_gb"]
        memory["devserver_reserve_gb"] = dev.json
        memory["jobs_now_gb"] = pyRound(jobsNow, 2).json
        memory["reserved_gb"] = pyRound(reserved, 2).json
        memory["others_gb"] = pyRound(pyMax(.double(0), ram - available - jobsNow), 2).json
        memory["free_for_admission_gb"] = pyRound(available - headroom - dev - reserved, 2).json
        memory["idle_max_gb"] = pyRound(idleTop - headroom, 1).json
        memory["swap_used_gb"] = pyRound(swapGB, 2).json
        // an entry someone else wrote may be short (Python would raise): it reads as no growth
        let first = swap[0].array.flatMap { $0.count > 1 ? PyNum($0[1]) : nil } ?? swapGB
        memory["swap_growth_2m_gb"] = pyRound(swapGB - first, 2).json
        memory["pressure"] = mem["pressure"] ?? .string("normal")
        memory["guard_level"] = guardLevel(snap, now: now).map { .int($0) } ?? .null
        memory["native"] = .object(PyObject([
            ("build_gb", pyRound(natGB, 2).json), ("active", .bool(nat["active"]?.truthy ?? false)), ("outside", .bool(outside)),
            ("reserve_gb", pyRound(nativeReserve, 2).json), ("owner", natTool.0), ("owner_label", natTool.1),
        ]))
        memory["simulators"] = simulatorsInfo(snap, now: now)
        memory["brake"] = .string(stageText[min(stage, 3)])
        memory["long_lived_gb"] = longLived
        memory["long_lived"] = families
        state["memory"] = .object(memory)
        var today = state["today"]?.object ?? PyObject()
        today["max_reserved_gb"] = pyRound(pyMax(PyNum(today["max_reserved_gb"]) ?? .int(0), reserved), 1).json
        today["peak_concurrency"] = pyMax(PyNum(today["peak_concurrency"]) ?? .int(0), .int(state["running"]?.array?.count ?? 0)).json
        state["today"] = .object(today)
        return memory
    }

    static func nativeOwnerInfo(_ owner: PyObject?) -> (PyJSON, PyJSON) {
        guard let owner else { return (.null, .null) }
        return (owner["id"] ?? .null, owner["label"] ?? .null)
    }

    // MARK: safety

    /// safety(state, cfg): SIGSTOP the youngest heavy job while swap grows, SIGCONT when it eases
    public static func safety(_ state: inout PyObject, _ cfg: SchedConfig, _ probes: SchedProbes) {
        let mem = state["memory"]?.object ?? PyObject()
        let growth = PyNum(mem["swap_growth_2m_gb"]) ?? .int(0)
        var running = (state["running"]?.array ?? []).compactMap(\.object)
        let localIdx = running.indices.filter { running[$0]["where"] == .string("local") && (running[$0]["child_pgid"]?.truthy ?? false) }
        let pausedIdx = localIdx.filter { running[$0]["paused"]?.truthy ?? false }
        let level = PyNum(mem["level_pct"]) ?? .int(0)
        let swapping = (growth.value > cfg.num("pause_swap_gb").value && level.value < 25) || mem["pressure"] == .string("critical")
        if swapping {
            let heavy = localIdx.filter { i in
                !(running[i]["paused"]?.truthy ?? false) && !(running[i]["small"]?.truthy ?? false) && running[i]["lang"] != .string("native")
            }
            guard localIdx.count - pausedIdx.count > 1, !heavy.isEmpty else { return }
            // max(heavy, key=started_at): the first of the youngest
            var victim = heavy[0]
            for i in heavy.dropFirst() where PyNum.or(running[i]["started_at"], .int(0)).value > PyNum.or(running[victim]["started_at"], .int(0)).value {
                victim = i
            }
            guard let pgid = running[victim]["child_pgid"]?.int, probes.killpg(pid_t(clamping: pgid), SIGSTOP) else { return }
            running[victim]["paused"] = true
            running[victim]["paused_at"] = .double(probes.now)
            running[victim]["pause_reason"] = .string("swap +\(GuardText.fixed(growth.value, 1)) GB in 2 min")
            var today = state["today"]?.object ?? PyObject()
            today["pauses"] = ((PyNum(today["pauses"]) ?? .int(0)) + .int(1)).json
            state["today"] = .object(today)
        } else if !pausedIdx.isEmpty && growth.value < 0.1 && level.value >= 30 {
            var job = pausedIdx[0]
            for i in pausedIdx.dropFirst() where PyNum.or(running[i]["paused_at"], .int(0)).value < PyNum.or(running[job]["paused_at"], .int(0)).value {
                job = i
            }
            if probes.now - PyNum.or(running[job]["paused_at"], .int(0)).value >= 30 {
                if let pgid = running[job]["child_pgid"]?.int { _ = probes.killpg(pid_t(clamping: pgid), SIGCONT) }
                running[job]["paused"] = false
                running[job]["pause_reason"] = .null
            }
        }
        state["running"] = .array(running.map { .object($0) })
    }
}
