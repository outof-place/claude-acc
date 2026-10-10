// sched.py's admission: plan (FIFO with a reservation for a starving head), backfill and shadow
// (EASY backfilling on the jobs' predicted ends), and update_queue_view (positions, reasons and
// start ETAs for the panel). Ported line by line; the order of every list follows the Python code.

extension Sched {
    /// queue_order(state): by enqueued_at, stable
    static func queueOrder(_ state: PyObject) -> [PyObject] {
        let q = (state["queue"]?.array ?? []).compactMap(\.object)
        return q.enumerated().sorted { l, r in
            let a = l.element["enqueued_at"]?.double ?? 0, b = r.element["enqueued_at"]?.double ?? 0
            return a != b ? a < b : l.offset < r.offset
        }.map(\.element)
    }

    /// measured(job): a prediction from the job's own history
    static func measured(_ job: PyObject) -> Bool {
        let from = job["predicted_from"] ?? .null
        let text: String = from.truthy ? (from.string ?? pyStr(from)) : ""
        return text.pyStarts("history")
    }

    public struct Shade {
        var waitS: PyNum?
        var spareGB: PyNum?
        var after: [PyJSON]
        var sure: Bool
        var guessed: Bool
    }

    /// shadow(state, need, free, now, ahead, lone)
    static func shadow(
        _ state: PyObject, need: PyNum, free: PyNum, now: Double, ahead: [(PyJSON, PyNum, PyNum?, Bool)] = [], lone: (PyNum, Bool)? = nil
    ) -> Shade {
        typealias End = (left: PyNum, i: Int, id: PyJSON, gb: PyNum, held: PyNum, sure: Bool)
        var ends: [End] = []
        var guessed: [PyJSON] = []
        let running = (state["running"]?.array ?? []).compactMap(\.object)
        for (i, r) in running.enumerated() {
            if r["where"] != .string("local") { continue }
            let wall = PyNum.or(r["predicted_wall_s"], .int(60))
            let over = (wall - .double(now - PyNum(r["started_at"]).map(\.value).orElse(now))).value < 0
            let held = PyNum.or(r["mem_now_gb"], .double(0))
            let sure = measured(r) && !over && !(r["stalled_s"]?.truthy ?? false)
            if !measured(r) || (r["stalled_s"]?.truthy ?? false) { guessed.append(r["id"] ?? .null) }
            ends.append((timeLeft(r, now), i, r["id"] ?? .null, held + growthLeft(r, now), held, sure))
        }
        for (k, a) in ahead.enumerated() {
            let (jid, gb, wall, sure) = a
            if !sure { guessed.append(jid) }
            let w = wall.flatMap { $0.truthy ? $0 : nil } ?? .int(60)
            ends.append((pyMax(.double(5.0), w), running.count + k, jid, gb, .double(0), sure && (wall?.truthy ?? false)))
        }
        ends = ends.enumerated().sorted { l, r in
            let a = l.element, b = r.element
            if a.left.value != b.left.value { return a.left.value < b.left.value }
            if a.i != b.i { return a.i < b.i }
            return l.offset < r.offset
        }.map(\.element)
        var out = Shade(waitS: .double(0), spareGB: free - need, after: [], sure: true, guessed: false)
        if need.value <= free.value { return out }
        var free = free
        var released: PyNum = .double(0)
        var unsure: [(PyJSON, PyNum)] = []
        for e in ends {
            free = free + e.gb
            released = released + e.held
            out.after.append(e.id)
            if !e.sure {
                if let i = unsure.firstIndex(where: { $0.0 == e.id }) { unsure[i].1 = e.gb } else { unsure.append((e.id, e.gb)) }
            }
            if need.value <= free.value {
                let idle = pySum(unsure.map(\.1))
                if idle.value <= (free - need).value {
                    out.after = out.after.filter { a in !unsure.contains { $0.0 == a } }
                    out.waitS = e.left
                    out.spareGB = free - need - idle
                } else {
                    out.waitS = e.left
                    out.spareGB = free - need
                    out.sure = false
                    out.guessed = guessed.contains { g in unsure.contains { $0.0 == g } }
                }
                return out
            }
        }
        if let lone, let last = ends.last, lone.1 || need.value <= (lone.0 + released).value {
            out.waitS = last.left
            out.spareGB = nil
            out.sure = unsure.isEmpty
            out.guessed = guessed.contains { g in unsure.contains { $0.0 == g } }
            return out
        }
        out.waitS = nil
        out.spareGB = nil
        out.sure = false
        return out
    }

    /// head_fits_without_passers(state, head, free, now, ahead, lone)
    static func headFitsWithoutPassers(
        _ state: PyObject, _ head: PyObject, free: PyNum, now: Double, ahead: [(PyJSON, PyNum, PyNum?, Bool)], lone: (PyNum, Bool)?
    ) -> Bool {
        let need = PyNum(head["mem_predicted_gb"]) ?? .int(0)
        let local = local(state)
        let passers = local.filter { $0["passed"] == head["id"] && $0["passed"] != nil }
        let gb = pySum(passers.map { PyNum.or($0["mem_now_gb"], .double(0)) + growthLeft($0, now) })
        if need.value <= (free + gb).value { return true }
        let others = !ahead.isEmpty || passers.count < local.count
        let held = pySum(passers.map { PyNum.or($0["mem_now_gb"], .double(0)) })
        guard let lone else { return false }
        return !others && (lone.1 || need.value <= (lone.0 + held).value)
    }

    /// blockers_eta(state, job): (seconds until the job fits, ids it waits on)
    static func blockersETA(_ state: PyObject, _ job: PyObject, now: Double) -> (PyNum, [PyJSON]) {
        let free = PyNum(state["memory"]?["free_for_admission_gb"]) ?? .int(0)
        let s = shadow(state, need: PyNum(job["mem_predicted_gb"]) ?? .int(0), free: free, now: now)
        return (s.waitS ?? .double(3600.0), s.after)
    }

    /// backfill(job, free, now_free, shade, stuck, cfg)
    static func backfill(_ job: PyObject, free: PyNum, nowFree: PyNum, shade: Shade, stuck: Bool, cfg: SchedConfig) -> String? {
        let need = PyNum(job["mem_predicted_gb"]) ?? .int(0)
        if !measured(job) { return nil }
        let light = need.value <= cfg.num("small_gb").value
        let fits = need.value <= free.value
        let fitsNow = fits || (light && need.value <= nowFree.value)
        let slack = PyNum.double(backfillSlack.0) * PyNum.or(job["predicted_wall_s"], .int(0)) + .double(backfillSlack.1)
        if let wait = shade.waitS, fitsNow, slack.value <= wait.value, shade.sure || (light && stuck && !shade.guessed) {
            return "ends"
        }
        if shade.sure {
            if fits, let spare = shade.spareGB, need.value <= spare.value { return "beside" }
            return nil
        }
        if stuck && light && fitsNow && slack.value <= cfg.num("head_delay_s").value { return "short" }
        return nil
    }

    /// plan(state, cfg, now): {id: ("fits"|"overtake", passed id)} in admission order
    public static func plan(_ state: PyObject, _ cfg: SchedConfig, now: Double) -> [(PyJSON, String, PyJSON)] {
        let mem = state["memory"]?.object ?? PyObject()
        var free = PyNum(mem["free_for_admission_gb"]) ?? .int(0)
        let running = (state["running"]?.array ?? []).compactMap(\.object)
        var nowFree = (PyNum(mem["available_gb"]) ?? .int(0)) - cfg.num("headroom_gb")
            - pySum(running.filter { $0["where"] == .string("local") && ($0["small"]?.truthy ?? false) }.map { growthLeft($0, now) })
        var anyLocal = running.contains { $0["where"] == .string("local") }
        var pressure = mem["pressure"]?.string ?? "normal"
        if mem["pressure"] == nil { pressure = "normal" }
        let guardCritical = mem["guard_level"] == .int(2)
        let brake = mem["brake"]?.string ?? (mem["brake"] == nil ? "normal" : "")
        var admitted: [(PyJSON, String, PyJSON)] = []
        var ahead: [(PyJSON, PyNum, PyNum?, Bool)] = []
        var blocked: PyObject?
        var shade: Shade?
        var stuck = false
        var reserve: PyNum = .double(0)
        var strict = false
        var slot = nativeOwner(state) != nil || (mem["native"]?["outside"]?.truthy ?? false)
        if pressure == "critical" || brake == "emergency" { return admitted }
        if brake == "tight" || brake == "brake" { pressure = "warn" }
        for job in queueOrder(state) {
            if job["route"]?["choice"] == .string("depot") { continue }
            if ((job["exclusive"]?.truthy ?? false) && slot) || simWait(job, mem) { continue }
            let need = PyNum(job["mem_predicted_gb"]) ?? .int(0)
            let native = job["lang"] == .string("native")
            let held = native && guardCritical
            let quick = (job["small"]?.truthy ?? false) && !strict && !native && need.value <= (nowFree - reserve).value
            let enq = job["enqueued_at"]?.double ?? 0
            if blocked == nil {
                let spare = (PyNum(mem["available_gb"]) ?? .int(0)) - cfg.num("headroom_gb")
                let alone = !anyLocal && admitted.isEmpty && brake != "brake"
                let overcommit = pressure != "warn" && !native && now - enq >= 30
                if !held && (need.value <= free.value || quick || (alone && (need.value <= spare.value || overcommit))) {
                    admitted.append((job["id"] ?? .null, "fits", .null))
                    ahead.append((job["id"] ?? .null, need, PyNum(job["predicted_wall_s"]), measured(job)))
                    free = free - need
                    nowFree = nowFree - need
                    anyLocal = true
                    slot = slot || (job["exclusive"]?.truthy ?? false)
                    continue
                }
                blocked = job
                if now - enq > cfg.num("starve_s").value {
                    reserve = need
                    strict = now - enq > 2 * cfg.num("starve_s").value
                }
                if !held {
                    let lone: (PyNum, Bool)? = brake != "brake" ? (spare, overcommit) : nil
                    shade = shadow(state, need: need, free: free, now: now, ahead: ahead, lone: lone)
                    stuck = !headFitsWithoutPassers(state, job, free: free, now: now, ahead: ahead, lone: lone)
                }
                continue
            }
            var how: String?
            if let s = shade, !native { how = backfill(job, free: free, nowFree: nowFree, shade: s, stuck: stuck, cfg: cfg) }
            let aged = !native && !held && now - enq >= cfg.num("aging_s").value
                && (need.value <= free.value || (need.value <= cfg.num("small_gb").value && need.value <= nowFree.value))
            if aged && how == nil { how = "aged" }
            if how != nil || ((job["small"]?.truthy ?? false) && !held && (need.value <= (free - reserve).value || quick)) {
                admitted.append((job["id"] ?? .null, "overtake", blocked!["id"] ?? .null))
                free = free - need
                nowFree = nowFree - need
                slot = slot || (job["exclusive"]?.truthy ?? false)
                if var s = shade, how != "ends", let spare = s.spareGB {
                    s.spareGB = spare - need
                    shade = s
                }
            }
        }
        return admitted
    }

    /// update_queue_view(state, cfg): positions, waiting reasons and start ETAs
    public static func updateQueueView(_ state: inout PyObject, _ cfg: SchedConfig, now: Double) {
        let mem = state["memory"]?.object ?? PyObject()
        var labels: [PyJSON: PyJSON] = [:]
        for j in ((state["running"]?.array ?? []) + (state["queue"]?.array ?? [])).compactMap(\.object) {
            labels[j["id"] ?? .null] = j["label"] ?? .null
        }
        var headBlocked: PyObject?
        let owner = nativeOwner(state)
        let native = mem["native"]?.object ?? PyObject()
        let stuckJobs = (state["running"]?.array ?? []).compactMap(\.object).filter { $0["stalled_s"]?.truthy ?? false }
        let stalled = stuckJobs.prefix(2).map { j in
            "\(pyStr(j["label"] ?? .null)) stalled \(humanS(PyNum(j["stalled_s"]) ?? .int(0))) (no CPU, no output), its reservation released"
        }.joined(separator: "; ")
        var updated: [PyJSON: PyObject] = [:]
        for (index, original) in queueOrder(state).enumerated() {
            var job = original
            job["position"] = .int(index + 1)
            let enq = job["enqueued_at"]?.double ?? 0
            job["waited_s"] = pyRound(.double(now - enq), 1).json
            var (wait, after) = blockersETA(state, job, now: now)
            job["eta_start_s"] = wait.value < 3600 ? .int(GuardText.round(wait.value)) : .null
            let free = pyMax(.double(0), PyNum(mem["free_for_admission_gb"]) ?? .int(0))
            var waitsTurn = false
            let code: String
            var text: String
            if mem["pressure"] == .string("critical") {
                code = "pressure"; text = "paused: memory pressure is critical"
            } else if mem["brake"] == .string("emergency") {
                code = "pressure"; text = "paused: the memory brake is freeing memory"
            } else if job["lang"] == .string("native") && mem["guard_level"] == .int(2) {
                code = "pressure"; text = "paused: the dev server guard sees critical memory pressure"
            } else if (job["exclusive"]?.truthy ?? false) && (owner != nil || (native["outside"]?.truthy ?? false)) {
                waitsTurn = true
                code = "native"
                if let owner {
                    var o = owner
                    o["predicted_wall_s"] = PyNum.or(owner["predicted_wall_s"], .int(600)).json
                    job["eta_start_s"] = .int(GuardText.round(timeLeft(o, now).value))
                    after = [owner["id"] ?? .null]
                    text = "waiting: one native build at a time, \(pyStr(owner["label"] ?? .null)) is building"
                } else {
                    job["eta_start_s"] = .null
                    let gb = PyNum(native["build_gb"]) ?? .int(0)
                    text = "waiting: a native build outside the scheduler is running (\(GuardText.fixed(gb.value, 1)) GB, xcodebuild)"
                }
            } else if simWait(job, mem) {
                waitsTurn = true
                code = "simulators"
                let sims = mem["simulators"]?.object ?? PyObject()
                job["eta_start_s"] = .null
                let holders = (sims["holders"]?.array ?? []).map(pyStr).joined(separator: ", ")
                text = "waiting: \(pyStr(sims["agents_in_use"] ?? .null)) agent simulators in use, cap \(pyStr(sims["cap"] ?? .null)) "
                    + "(\(holders)); one frees when its session runs `portivo-mobile release` or ends"
            } else if let head = headBlocked, now - (head["enqueued_at"]?.double ?? 0) > cfg.num("starve_s").value,
                      (PyNum(job["mem_predicted_gb"]) ?? .int(0)).value <= free.value
            {
                code = "head"
                let eta = head["eta_start_s"] ?? .null
                text = "waiting: \(pyStr(head["label"] ?? .null)) goes first"
                    + (eta.truthy ? " (starts in about \(humanS(PyNum(eta) ?? .int(0))))" : "") + ", starting now could delay it"
            } else {
                code = "memory"
                let need = PyNum(job["mem_predicted_gb"]) ?? .int(0)
                text = "waiting for \(GuardText.fixed(need.value, 1)) GB, \(GuardText.fixed(free.value, 1)) free"
                let names = after.prefix(2).map { a in pyStr(labels[a] ?? a) }.joined(separator: ", ")
                if !names.isEmpty {
                    text += " · starts when \(names) ends" + (wait.value < 3600 ? ", about \(humanS(wait))" : "")
                }
                if !stalled.isEmpty { text += " · \(stalled)" }
            }
            job["reason"] = .object(PyObject([
                ("code", .string(code)), ("need_gb", job["mem_predicted_gb"] ?? .null), ("free_gb", mem["free_for_admission_gb"] ?? .null),
                ("after", .array(after)), ("text", .string(text)),
            ]))
            if headBlocked == nil && !waitsTurn { headBlocked = job }
            updated[job["id"] ?? .null] = job
        }
        // the dicts are updated in place in Python: the queue keeps its own order
        state["queue"] = .array((state["queue"]?.array ?? []).map { j in
            guard let id = j["id"], let u = updated[id] else { return j }
            return .object(u)
        })
    }

    /// human_s(seconds)
    static func humanS(_ seconds: PyNum) -> String {
        let s = GuardText.round(seconds.value)
        if s < 60 { return "\(s)s" }
        let (m, sec) = (s / 60, s % 60)
        if m < 60 { return sec != 0 ? "\(m)m" + (sec < 10 ? "0" : "") + "\(sec)s" : "\(m)m" }
        let (h, mm) = (m / 60, m % 60)
        return "\(h)h" + (mm < 10 ? "0" : "") + "\(mm)m"
    }
}

extension Optional where Wrapped == Double {
    func orElse(_ value: Double) -> Double { self ?? value }
}

extension PyJSON: Hashable {
    public func hash(into hasher: inout Hasher) { hasher.combine(dumps()) }
}
