// One pass of the scheduler's housekeeping for every job at once: what each waiting wrapper does
// twice a second and each running one once a second today (reap, refresh_memory, safety, plan,
// update_queue_view), here once. The replay feeds it recorded or synthetic states and compares
// with sched.py's own functions run in the same order (tests/acc_cored/sched_replay.py).
import Darwin

extension Sched {
    public struct TickResult {
        /// plan()'s answer: (job id, "fits" or "overtake", the id it passed)
        public var admitted: [(PyJSON, String, PyJSON)]
    }

    public static func tick(_ state: inout PyObject, _ cfg: SchedConfig, _ probes: SchedProbes) -> TickResult {
        reap(&state, probes)
        refreshMemory(&state, cfg, probes)
        safety(&state, cfg, probes)
        let admitted = plan(state, cfg, now: probes.now)
        updateQueueView(&state, cfg, now: probes.now)
        return TickResult(admitted: admitted)
    }
}

/// A tick's readings served from a fixture, the killpg calls kept.
public final class RecordedSchedProbes: SchedProbes {
    let f: PyObject
    public let now: Double
    public private(set) var kills: [PyJSON] = []

    public init(_ fixture: PyObject) {
        f = fixture
        now = fixture["now"]?.double ?? 0
    }

    public func memory() -> PyObject { f["memory"]?.object ?? PyObject() }
    public func guardSnapshot() -> PyObject { f["snapshot"]?.object ?? PyObject() }
    public func guardMaxServerGB() -> PyJSON? { f["max_server_gb"].flatMap { $0.isNull ? nil : $0 } }

    public func nativeScan() -> (gb: Double, active: Bool, pids: Set<pid_t>) {
        let s = f["native_scan"]?.object ?? PyObject()
        return (s["gb"]?.double ?? 0, s["active"]?.truthy ?? false, Set((s["pids"]?.array ?? []).compactMap { $0.int.map { pid_t($0) } }))
    }

    public func alive(_ pid: PyJSON?) -> Bool {
        guard let pid, pid.truthy else { return false }
        return (f["alive"]?.array ?? []).contains(pid)
    }

    public func descendants(_ pid: pid_t) -> Set<pid_t> {
        Set((f["descendants"]?[String(pid)]?.array ?? [.int(Int(pid))]).compactMap { $0.int.map { pid_t($0) } })
    }

    public func killpg(_ pgid: pid_t, _ signal: Int32) -> Bool {
        kills.append(.array([.int(Int(pgid)), .int(Int(signal))]))
        return !(f["killpg_fails"]?.truthy ?? false)
    }

    public func day() -> String { f["day"]?.string ?? "" }
}

extension Sched {
    /// One fixture through the native tick; the differences from sched.py's answer.
    public static func replay(_ fixture: PyObject) -> [String] {
        var diffs: [String] = []
        let probes = RecordedSchedProbes(fixture)
        var cfg = SchedConfig.defaults
        for (k, v) in fixture["cfg"]?.object ?? PyObject() { cfg[k] = v }
        var state = fixture["state_before"]?.object ?? PyObject()
        let result = tick(&state, SchedConfig(raw: cfg), probes)
        let expect = fixture["expect"]?.object ?? PyObject()
        GuardReplay.compare("state", .object(state), expect["state_after"] ?? .null, into: &diffs)
        let admitted = PyJSON.array(result.admitted.map { .array([$0.0, .string($0.1), $0.2]) })
        GuardReplay.compare("admitted", admitted, expect["admitted"] ?? .null, into: &diffs)
        GuardReplay.compare("kills", .array(probes.kills), expect["kills"] ?? .array([]), into: &diffs)
        return diffs
    }
}
