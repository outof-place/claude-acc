// The parity replay: a fixture from tests/acc_cored/devguard_replay.py (a recorded or synthetic tick
// with what Python decided) through the native tick, and the differences. `acc-cored guard-replay`.
import Darwin

/// A tick's readings served from a fixture; a reading the fixture doesn't have is noted as missing.
public final class RecordedProbes: GuardProbes {
    let f: PyObject
    public let now: Double
    public let absolute: UInt64
    public let home: String
    public private(set) var missing: [String] = []

    public init(_ fixture: PyObject) {
        f = fixture
        now = fixture["now"]?.double ?? 0
        absolute = UInt64(fixture["absolute"]?.int ?? 0)
        home = fixture["home"]?.string ?? "/"
    }

    func keyed(_ table: String, _ key: String) -> PyJSON? {
        guard let value = f[table]?[key] else {
            missing.append("\(table):\(key)")
            return nil
        }
        return value
    }

    public func processes() -> [ProcRow] {
        (f["rows"]?.array ?? []).compactMap { r in
            guard let a = r.array, a.count == 3, let pid = a[0].int, let ppid = a[1].int, let cmd = a[2].string else { return nil }
            return ProcRow(pid: pid_t(pid), ppid: pid_t(ppid), command: cmd)
        }
    }

    public func sockets() -> SocketTable {
        var table = SocketTable()
        let s = f["sockets"] ?? .null
        for (port, pids) in s["listen"]?.object ?? PyObject() {
            table.listen[Int(port) ?? 0] = Set((pids.array ?? []).compactMap { $0.int.map { pid_t($0) } })
        }
        table.links = (s["links"]?.array ?? []).compactMap { l in
            guard let a = l.array, a.count == 2, let pid = a[0].int, let port = a[1].int else { return nil }
            return SocketTable.Link(pid: pid_t(pid), port: port)
        }
        return table
    }

    private var used: [String: Int] = [:]

    public func usage(_ pid: pid_t) -> Usage? {
        let k = String(pid)
        let value: PyJSON?
        if let seq = f["usage_seq"]?[k]?.array, !seq.isEmpty {
            let i = used[k, default: 0]
            used[k] = i + 1
            value = seq[min(i, seq.count - 1)]
        } else {
            value = keyed("usage", k)
        }
        guard let v = value, let o = v.object else { return nil }
        return Usage(
            footprint: UInt64(o["footprint"]?.int ?? 0), peak: UInt64(o["peak"]?.int ?? 0), cpu: o["cpu"]?.double ?? 0,
            written: UInt64(o["written"]?.int ?? 0), start: UInt64(o["start"]?.int ?? 0))
    }

    public func cwd(_ pid: pid_t) -> String? { keyed("cwd", String(pid))?.string }

    public func argv(_ pid: pid_t) -> [String]? {
        keyed("argv", String(pid))?.array?.compactMap(\.string)
    }

    public func sysctl(_ name: String) -> UInt64? {
        guard let v = keyed("sysctl", name), let i = v.int else { return nil }
        return UInt64(i)
    }

    public func swap() -> (total: UInt64, used: UInt64) {
        let a = f["swap"]?.array ?? []
        return (UInt64(a.first?.int ?? 0), UInt64(a.count > 1 ? a[1].int ?? 0 : 0))
    }

    public func startEpoch(_ pid: pid_t) -> Int? { keyed("start_epoch", String(pid))?.int }

    public func json(_ path: String, fallback: PyJSON) -> PyJSON {
        guard let v = f["json"]?[path], !v.isNull else { return fallback }
        return v
    }

    public func simulatorName(_ udid: String) -> String? {
        // devguard_core.sim_name falls back to the udid itself
        keyed("sim_names", udid)?.string
    }

    public func orca(_ read: OrcaRead) -> PyJSON? {
        guard let v = f["orca_calls"]?[read.rawValue] else {
            missing.append("orca:\(read.rawValue)")
            return nil
        }
        return v.isNull ? nil : v
    }

    public var orcaMarker: String? { f["marker"]?.string }
}

public enum GuardReplay {
    public struct Outcome {
        public var differences: [String] = []
        public var missing: [String] = []
        /// a devguard.json value of another type than the default: the engine hands such ticks over
        public var configHandedOver = false
    }

    /// One fixture through the native tick; the differences from what Python decided.
    public static func run(_ fixture: PyObject) -> Outcome {
        var out = Outcome()
        let probes = RecordedProbes(fixture)
        var cfg = GuardConfig.defaults
        for (k, v) in fixture["cfg"]?.object ?? PyObject() { cfg[k] = v }
        let config = GuardConfig(raw: cfg)
        if !config.mismatched.isEmpty {
            out.configHandedOver = true
            return out
        }
        GuardPaths.fsguardState = fixture["fsguard"]?.string ?? GuardPaths.fsguardState
        let orca = OrcaView(fixture["orca_before"]?.object ?? PyObject())
        var state = fixture["state_before"]?.object ?? PyObject()
        GuardLog.capture = []
        defer { GuardLog.capture = nil }
        let result = guardTick(cfg: config, state: &state, orca: orca, probes: probes, enforce: config["mode"] == .string("enforce"))
        let expect = fixture["expect"]?.object ?? PyObject()
        compare("state", .object(state), expect["state_after"] ?? .null, into: &out.differences)
        compare("orca", orca.dump(), expect["orca_after"] ?? .null, into: &out.differences, numbers: .lenient)
        let plans = PyJSON.array(result.plans.map { $0.summary(home: probes.home) })
        compare("plans", plans, expect["plans"] ?? .null, into: &out.differences)
        // Python's recorder declines every plan, so its loop hands each one to execute() in order and
        // then brakes as with nothing acted: the native list and brake flag must say the same
        let calls = PyJSON.array(result.actable.map(callJSON))
        if let pyCalls = expect["calls"] {
            compare("calls", calls, pyCalls, into: &out.differences)
        } else {
            let act = result.actable.first.map(callJSON) ?? .null
            if act != expect["act"] ?? .null { out.differences.append("act: native \(act.dumps()) python \((expect["act"] ?? .null).dumps())") }
        }
        let brake: PyJSON = result.brakeDue ? .int(result.world.pressure.stage) : .null
        let pyBrake = expect["brake"] ?? .null
        if brake != pyBrake { out.differences.append("brake: native \(brake.dumps()) python \(pyBrake.dumps())") }
        let logs = PyJSON.array((GuardLog.capture ?? []).filter { $0.0 == "log" }.map { .string($0.1) })
        compare("log", logs, expect["log"] ?? .array([]), into: &out.differences)
        out.missing = probes.missing
        return out
    }

    static func callJSON(_ p: Plan) -> PyJSON {
        .object(PyObject([("unit", .string(p.key)), ("action", .string(p.action)), ("code", .string(p.code))]))
    }

    /// Semantic JSON equality (key order aside), with floats equal to 1e-12 relative: Python walks a
    /// simulator's tree as a set, so its compensated CPU sum may differ in the last bits.
    public enum Numbers { case strict, lenient }

    public static func compare(_ path: String, _ a: PyJSON, _ b: PyJSON, into diffs: inout [String], numbers: Numbers = .strict) {
        if diffs.count > 40 { return }
        switch (a, b) {
        case (.object(let x), .object(let y)):
            for k in Set(x.keys).union(y.keys).sorted() {
                compare(path + "." + k, x[k] ?? .string("<absent>"), y[k] ?? .string("<absent>"), into: &diffs, numbers: numbers)
            }
        case (.array(let x), .array(let y)):
            if x.count != y.count {
                diffs.append("\(path): \(x.count) items native, \(y.count) python: \(a.dumps().prefix(300)) | \(b.dumps().prefix(300))")
                return
            }
            for (i, (p, q)) in zip(x, y).enumerated() { compare("\(path)[\(i)]", p, q, into: &diffs, numbers: numbers) }
        case (.int(let x), .double(let y)) where numbers == .lenient && Double(x) == y: break
        case (.double(let x), .int(let y)) where numbers == .lenient && x == Double(y): break
        case (.double(let x), .double(let y)):
            if x != y && !(Swift.abs(x - y) <= 1e-12 * max(Swift.abs(x), Swift.abs(y))) && !(x.isNaN && y.isNaN) {
                diffs.append("\(path): \(PyJSON.repr(x)) native, \(PyJSON.repr(y)) python")
            }
        default:
            if a != b { diffs.append("\(path): \(a.dumps().prefix(300)) native, \(b.dumps().prefix(300)) python") }
        }
    }
}

extension OrcaView {
    /// From the replay's orca_state()
    public convenience init(_ s: PyObject) {
        self.init()
        ok = s["ok"]?.truthy ?? false
        at = s["at"]?.double ?? 0
        sessionsAt = s["sessions_at"]?.double ?? 0
        tabs = (s["tabs"]?.array ?? []).compactMap(\.object)
        worktrees = (s["worktrees"]?.array ?? []).compactMap(\.object)
        terminals = (s["terminals"]?.array ?? []).compactMap(\.object)
        for pair in s["sessions"]?.array ?? [] {
            if let a = pair.array, a.count == 2, let k = a[0].int { sessions[k] = a[1] }
        }
    }

    public func dump() -> PyJSON {
        .object(PyObject([
            ("ok", .bool(ok)), ("at", at == 0 ? .int(0) : .double(at)), ("sessions_at", sessionsAt == 0 ? .int(0) : .double(sessionsAt)),
            ("tabs", .array(tabs.map { .object($0) })), ("worktrees", .array(worktrees.map { .object($0) })),
            ("terminals", .array(terminals.map { .object($0) })),
            ("sessions", .array(sessions.keys.sorted().map { .array([.int($0), sessions[$0]!]) })),
        ]))
    }
}
