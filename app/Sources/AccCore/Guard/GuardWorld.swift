// devguard_core's picture of the Mac in one tick, ported line by line: dev servers and the units
// that started them (discover, Server, Unit), simulators, memory pressure, Orca's tabs and
// terminals, and what the state file remembers of each unit. The order of every list follows the
// Python code, because labels, sums and "the first plan" depend on it.
import Darwin

// MARK: - pieces

public final class Server {
    public let pid: pid_t
    public let kind: String
    public let command: String
    public let tree: [pid_t]
    public let cwd: String
    public let footprint: Int
    public let peak: Int
    public let regrow: Int
    public let cpu: PyNum
    public let written: Int
    public var ports: [Int] = []

    init(pid: pid_t, kind: String, command: String, tree: [pid_t], probes: GuardProbes) {
        self.pid = pid
        self.kind = kind
        self.command = command
        self.tree = tree
        cwd = probes.cwd(pid) ?? "?"
        let stats = tree.compactMap { probes.usage($0) }
        footprint = stats.reduce(0) { $0 + Int($1.footprint) }
        peak = stats.map { Int($0.peak) }.max() ?? 0
        regrow = stats.reduce(0) { $0 + max(0, Int($1.peak) - Int($1.footprint)) }
        cpu = pySum(stats.map(\.cpu))
        written = stats.reduce(0) { $0 + Int($1.written) }
    }
}

/// A row of the table: (ppid, command)
public typealias TableRow = (ppid: pid_t, command: String, facts: CommandFacts)

/// The table as a dict in Python: pid -> row, keeping the rows' order.
public struct ProcTable {
    public var order: [pid_t] = []
    public var rows: [pid_t: TableRow] = [:]

    public init(_ list: [ProcRow]) {
        for r in list {
            if rows[r.pid] == nil { order.append(r.pid) }
            rows[r.pid] = (r.ppid, r.command, r.facts)
        }
    }

    public subscript(pid: pid_t) -> TableRow? { rows[pid] }
    public func ppid(_ pid: pid_t) -> pid_t { rows[pid]?.ppid ?? 0 }
    public func command(_ pid: pid_t) -> String { rows[pid]?.command ?? "" }
    /// facts of the pid's line ("" for a pid not in the table)
    public func facts(_ pid: pid_t) -> CommandFacts { rows[pid]?.facts ?? Self.empty }
    static let empty = CommandFacts("")

    /// {ppid: [pid, ...]} in table order
    public func children() -> [pid_t: [pid_t]] {
        var out: [pid_t: [pid_t]] = [:]
        for pid in order { out[rows[pid]!.ppid, default: []].append(pid) }
        return out
    }
}

public func ancestors(_ pid: pid_t, _ table: ProcTable) -> [pid_t] {
    var out: [pid_t] = []
    var pid = pid
    while pid > 1, let row = table[pid], out.count < 64 {
        out.append(pid)
        pid = row.ppid
    }
    return out
}

public func descendants(_ pid: pid_t, _ children: [pid_t: [pid_t]]) -> [pid_t] {
    var out = [pid]
    var stack = [pid]
    while let top = stack.popLast() {
        for child in children[top] ?? [] {
            out.append(child)
            stack.append(child)
        }
    }
    return out
}

public final class Unit {
    public let root: pid_t
    public let servers: [Server]
    public let pids: [pid_t]
    public let start: UInt64
    public let key: String
    public let argv: [String]?
    public let launchCwd: String
    public let shell: pid_t?
    public let host: String
    public let ancestors: [pid_t]
    public let footprint: Int
    public let biggest: Int
    public let peak: Int
    public let regrow: Int
    public var background = false
    public let cpu: PyNum
    public var ports: [Int]
    public var clients: [Client] = []
    public var idleSimClients: [Client] = []
    public var tabs: [PyObject] = []
    public var terminal: PyObject?
    public var worktree: PyObject?
    public var consumers: [PyObject] = []
    public var protected = false
    public var pin: PyObject?
    public var age = 0.0
    public var started = 0.0
    public var quiet = 0.0
    public var lastWatched: PyNum = .int(0)
    /// set by the executor when a recycle killed the server (never in the native tick)
    public var killed = false

    public struct Client: Equatable {
        public var pid: pid_t
        public var kind: String
        public var name: String
    }

    init(root: pid_t, servers: [Server], table: ProcTable, children: [pid_t: [pid_t]], probes: GuardProbes) {
        self.root = root
        self.servers = servers
        pids = descendants(root, children).sorted()
        start = probes.usage(root)?.start ?? 0
        key = "\(root):\(start)"
        argv = probes.argv(root)
        launchCwd = probes.cwd(root) ?? servers[0].cwd
        let parent = table.ppid(root)
        let parentFacts = table.facts(parent)
        shell = parentFacts.shell ? parent : nil
        if parent <= 1 {
            host = "orphan"
        } else if shell != nil {
            host = "shell"
        } else if parentFacts.agent {
            host = "agent"
        } else {
            host = "other"
        }
        ancestors = AccCore.ancestors(root, table)
        footprint = servers.reduce(0) { $0 + $1.footprint }
        biggest = servers.map(\.footprint).max()!
        peak = max(servers.map(\.peak).max()!, footprint)
        regrow = servers.reduce(0) { $0 + $1.regrow }
        cpu = Self.sum(servers.map(\.cpu))
        ports = Array(Set(servers.flatMap(\.ports))).sorted()
    }

    /// sum() over values that are each an int 0 or a float
    static func sum(_ xs: [PyNum]) -> PyNum {
        // the int fast path until the first float, then the compensated sum, as builtin sum does
        var i = 0
        var total = 0
        while i < xs.count, case .int(let v) = xs[i] {
            total += v
            i += 1
        }
        if i == xs.count { return .int(total) }
        var floats = [Double(total) + xs[i].value]
        floats += xs[(i + 1)...].map(\.value)
        // the first float joined the int total by plain addition; pySum adds it to 0 the same way
        return pySum(floats)
    }

    public var appKey: String { pySorted(Set(servers.map(\.cwd))).joined(separator: "|") }

    public func label(home: String) -> String {
        let port = ports.first.map { ":\($0)" } ?? "pid \(servers[0].pid)"
        if servers.count == 1 { return "\(port) \(GuardText.short(servers[0].cwd, home: home))" }
        let list = ports.prefix(6).map { ":\($0)" }.joined(separator: ",")
        return "stos \(servers.count) serwerów \(list.isEmpty ? port : list) \(GuardText.short(launchCwd, home: home))"
    }

    public var watched: Bool { !clients.isEmpty || !tabs.isEmpty }

    public var attended: Bool {
        clients.contains { $0.kind == "browser" } || tabs.contains { $0["focused"]?.truthy ?? false }
    }

    public var agentWorking: Bool {
        let list: [PyObject?] = [worktree] + consumers.map { Optional($0) }
        return list.contains { w in
            guard let w, !w.isEmpty else { return false }
            if w["status"] == .string("working") { return true }
            return (w["agents"]?.array ?? []).contains { $0["state"] == .string("working") }
        }
    }

    public var recyclable: Bool {
        host == "shell" && !(terminal?.isEmpty ?? true) && !(terminal?["agentIdentity"]?.truthy ?? false) && !(argv?.isEmpty ?? true)
    }

    public func summary(home: String) -> PyJSON {
        var o = PyObject()
        o["key"] = .string(key)
        o["label"] = .string(label(home: home))
        o["root"] = .int(Int(root))
        o["pids"] = .array(pids.map { .int(Int($0)) })
        o["ports"] = .array(ports.map { .int($0) })
        o["kinds"] = .array(pySorted(Set(servers.map(\.kind))).map { .string($0) })
        o["cwd"] = .array(servers.map { .string($0.cwd) })
        o["footprint"] = .int(footprint)
        o["biggest"] = .int(biggest)
        o["peak"] = .int(peak)
        o["regrow"] = .int(regrow)
        o["host"] = .string(host)
        o["command"] = argv.flatMap { $0.isEmpty ? nil : PyJSON.string(GuardText.join($0)) } ?? .null
        o["terminal"] = terminal?["title"] ?? .null
        o["worktree"] = worktree?["path"] ?? .null
        o["clients"] = .array(clients.map { .object(PyObject([("pid", .int(Int($0.pid))), ("kind", .string($0.kind)), ("name", .string($0.name))])) })
        o["idle_sim_clients"] = .int(idleSimClients.count)
        o["tabs"] = .array(tabs.map { .object(PyObject([("url", $0["url"] ?? .null), ("focused", $0["focused"] ?? .null)])) })
        o["attended"] = .bool(attended)
        o["agent_working"] = .bool(agentWorking)
        o["recyclable"] = .bool(recyclable)
        o["age"] = .int(GuardText.round(age))
        o["quiet"] = .int(GuardText.round(quiet))
        o["protected"] = .bool(protected)
        o["pin"] = pin.map { .object($0) } ?? .null
        o["background"] = .bool(background)
        o["servers"] = .int(servers.count)
        o["launch_cwd"] = .string(launchCwd)
        return .object(o)
    }
}

// MARK: - discover


public func discover(_ cfg: GuardConfig, _ table: ProcTable, probes: GuardProbes) -> [Unit] {
    let children = table.children()
    var found: [(pid_t, String)] = []
    var foundSet: Set<pid_t> = []
    let runtimes = cfg.strings("runtimes")
    for pid in table.order {
        let facts = table.facts(pid)
        guard runtimes.contains(facts.program) else { continue }
        if let kind = facts.serverKind {
            found.append((pid, kind))
            foundSet.insert(pid)
        }
    }
    let scope = cfg.strings("scope").map { Files.expand($0, home: probes.home) }
    var servers: [Server] = []
    for (pid, kind) in found {
        // a server under another server of the same kind is its child, not a server of its own
        if ancestors(table.ppid(pid), table).contains(where: foundSet.contains) { continue }
        let server = Server(pid: pid, kind: kind, command: table.command(pid), tree: descendants(pid, children), probes: probes)
        if !scope.isEmpty, !scope.contains(where: { server.cwd == $0 || server.cwd.pyStarts($0 + "/") }) { continue }
        servers.append(server)
    }
    var groups: [(pid_t, [Server])] = []
    var index: [pid_t: Int] = [:]
    for server in servers {
        var root = server.pid
        while true {
            let parent = table.ppid(root)
            if parent <= 1 || !table.facts(parent).launcher { break }
            root = parent
        }
        if let i = index[root] {
            groups[i].1.append(server)
        } else {
            index[root] = groups.count
            groups.append((root, [server]))
        }
    }
    return groups.map { Unit(root: $0.0, servers: $0.1, table: table, children: children, probes: probes) }
}

// MARK: - simulators

public final class Simulator {
    public let udid: String
    public let root: pid_t
    public let pids: [pid_t]
    public let name: String
    public let key: String
    public let label: String
    public let pool: Bool
    public let lease: PyObject?
    public let leaseAlive: Bool
    public let watchers: [pid_t]
    public let protected: Bool
    public let footprint: Int
    public let cpu: PyNum
    public let start: UInt64
    public var age = 0.0
    public var quiet = 0.0

    init(cfg: GuardConfig, udid: String, root: pid_t, tree: Set<pid_t>, watchers: Set<pid_t>, probes: GuardProbes) {
        self.udid = udid
        self.root = root
        pids = tree.sorted()
        let name = probes.simulatorName(udid) ?? udid
        self.name = name
        key = "sim:\(udid)"
        label = "symulator \(name)"
        pool = name.pyStarts(cfg["simulator_pool_prefix"].string ?? "")
        let leasePath = Files.expand(cfg["simulator_leases"].string ?? "", home: probes.home) + "/" + udid + ".json"
        lease = probes.json(leasePath, fallback: .null).object
        leaseAlive = Simulator.alive(lease, probes: probes)
        self.watchers = watchers.sorted()
        protected = cfg.strings("simulator_protect").contains { udid == $0 || fnmatchcase(name, $0) }
        // Python walks the tree as a set (hash order); the ints don't care, the float sum may differ in
        // its last bit, which the replay allows
        let stats = pids.compactMap { probes.usage($0) }
        footprint = stats.reduce(0) { $0 + Int($1.footprint) }
        cpu = pySum(stats.map(\.cpu))
        start = probes.usage(root)?.start ?? 0
    }

    public var watched: Bool { !watchers.isEmpty }
    public var inUse: Bool { leaseAlive || watched || !pool || protected }

    /// lease_alive: the same pid and start portivo-mobile wrote (`ps -o lstart=`)
    static func alive(_ lease: PyObject?, probes: GuardProbes) -> Bool {
        guard let owner = lease?["owner"]?.object else { return false }
        let pid: Int
        switch owner["pid"] {
        case .int(let i)?: pid = i
        case .double(let d)? where d.isFinite: pid = Int(d)
        case .string(let s)?: guard let i = Int(s.trimmingPySpace()) else { return false }; pid = i
        case .bool(let b)?: pid = b ? 1 : 0
        default: return false
        }
        guard pid > 0, let started = probes.startEpoch(pid_t(clamping: pid)) else { return false }
        let raw: String = {
            switch owner["start"] {
            case nil, .null?: return ""
            case .string(let s)?: return s
            case let other?: return other.truthy ? other.dumps() : ""
            }
        }()
        let text = raw.split(whereSeparator: { $0.unicodeScalars.allSatisfy(pyIsSpace) }).joined(separator: " ")
        if text.isEmpty { return true }
        guard let want = lstartEpoch(text) else { return true }
        return Swift.abs(want - Double(started)) <= 2
    }

    public func summary() -> PyJSON {
        let owner = lease?["owner"]?.object ?? PyObject()
        var o = PyObject()
        o["key"] = .string(key)
        o["udid"] = .string(udid)
        o["name"] = .string(name)
        o["pool"] = .bool(pool)
        o["footprint"] = .int(footprint)
        o["processes"] = .int(pids.count)
        o["lease_app"] = lease?["app"] ?? .null
        let session = owner["session"] ?? .null
        o["lease_session"] = session.truthy ? session : .null
        o["lease_alive"] = .bool(leaseAlive)
        o["watchers"] = .array(watchers.map { .int(Int($0)) })
        o["in_use"] = .bool(inUse)
        o["protected"] = .bool(protected)
        o["age"] = .int(GuardText.round(age))
        o["quiet"] = .int(GuardText.round(quiet))
        return .object(o)
    }
}

/// time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")) in the C locale, nil when it doesn't parse
func lstartEpoch(_ text: String) -> Double? {
    var tm = tm()
    let rest = text.withCString { strptime($0, "%a %b %d %H:%M:%S %Y", &tm) }
    guard let rest, rest.pointee == 0 else { return nil }
    tm.tm_isdst = -1
    let t = mktime(&tm)
    return t == -1 ? nil : Double(t)
}

/// fnmatch.fnmatchcase: * ? [seq] [!seq], case-sensitive
public func fnmatchcase(_ name: String, _ pattern: String) -> Bool {
    fnmatch(pattern, name, 0) == 0
}

public func discoverSimulators(_ cfg: GuardConfig, _ list: [ProcRow], probes: GuardProbes) -> [Simulator] {
    var children: [pid_t: [pid_t]] = [:]
    for r in list { children[r.ppid, default: []].append(r.pid) }
    var roots: [(pid_t, String)] = []
    for r in list where r.ppid == 1 && r.facts.launchdSim {
        if let udid = r.facts.simDevice { roots.append((r.pid, udid)) }
    }
    if roots.isEmpty { return [] }
    var trees: [pid_t: Set<pid_t>] = [:]
    for (pid, _) in roots { trees[pid] = Set(descendants(pid, children)) }
    let inside = trees.values.reduce(into: Set<pid_t>()) { $0.formUnion($1) }
    let outside = list.filter { !inside.contains($0.pid) }
    let viewers = outside.filter { !$0.facts.hasUdid && $0.facts.simViewer }.map(\.pid)
    var seen: Set<pid_t> = []
    var sims: [Simulator] = []
    for (root, udid) in roots where seen.insert(root).inserted {
        let watchers = Set(outside.filter { $0.command.pyContains(udid) }.map(\.pid)).union(viewers)
        sims.append(Simulator(cfg: cfg, udid: udid, root: root, tree: trees[root]!, watchers: watchers, probes: probes))
    }
    return sims
}

// MARK: - pressure

public struct Pressure {
    public var ram: Int
    public var kernel: Int
    public var available: Int?
    public var swapTotal: Int
    public var swapUsed: Int
    public var compressed: Int
    public var segments: Int?
    public var segmentsLimit: Int?
    public var swapGrowth: Int
    public var swapouts: Int
    public var swapping: Bool
    public var reasons: [String] = []
    public var notes: [String] = []
    public var level = 0
    public var stage = 0
    public var stageReasons: [String] = []

    /// Pressure(cfg, state, now): reads the kernel, appends to state["swap_history"]
    public init(cfg: GuardConfig, state: inout PyObject, now: Double, probes: GuardProbes) {
        ram = probes.sysctl("hw.memsize").map { Int($0) } ?? 16 * GuardText.GB
        kernel = probes.sysctl("kern.memorystatus_vm_pressure_level").map { Int($0) }.flatMap { $0 == 0 ? nil : $0 } ?? 1
        let kernelUsed = cfg["kernel_pressure"].truthy || cfg.raw["kernel_pressure"] == nil ? kernel : 1
        available = probes.sysctl("kern.memorystatus_level").map { Int($0) }
        let swap = probes.swap()
        swapTotal = Int(swap.total)
        swapUsed = Int(swap.used)
        compressed = probes.sysctl("vm.compressor_bytes_used").map { Int($0) }.flatMap { $0 == 0 ? nil : $0 } ?? 0
        segments = probes.sysctl("vm.compressor.segment.total").map { Int($0) }
        segmentsLimit = probes.sysctl("vm.compressor.segment.limit").map { Int($0) }
        let swapoutsNow = probes.sysctl("vm.compressor.compactor.swapouts_queued_pressure").map { Int($0) }
        var history: [PyJSON] = (state["swap_history"]?.array ?? []).filter { h in
            guard let t = h.array?.first?.double else { return false }
            return now - t <= 120
        }
        history.append(.array([.double(now), .int(swapUsed), swapoutsNow.map { .int($0) } ?? .null]))
        state["swap_history"] = .array(history)
        let first = history[0].array!
        swapGrowth = swapUsed - (first[1].int ?? Int(first[1].double ?? 0))
        if let s = swapoutsNow, let f = first[2].int {
            swapouts = s - f
        } else {
            swapouts = 0
        }
        swapping = Double(swapGrowth) >= cfg.number("swapping_mb") * Double(GuardText.MB) || Double(swapouts) >= cfg.number("swapouts_burst")
        let warn = cfg.number("swap_warn_percent") / 100 * Double(ram)
        let critical = cfg.number("swap_critical_percent") / 100 * Double(ram)
        let avail = available ?? 100
        if kernelUsed >= 4 { reasons.append("jądro: presja krytyczna") }
        if Double(avail) <= cfg.number("available_critical_percent") { reasons.append("dostępne tylko \(avail)% pamięci") }
        if Double(swapUsed) >= critical && swapping { reasons.append("swap \(GuardText.human(swapUsed)) i rośnie") }
        if !reasons.isEmpty {
            level = 2
            computeStage(cfg: cfg, kernel: kernelUsed)
            return
        }
        if kernelUsed >= 2 { reasons.append("jądro: ostrzeżenie o presji") }
        if Double(avail) <= cfg.number("available_warn_percent") { reasons.append("dostępne \(avail)% pamięci") }
        if Double(swapUsed) >= warn && swapping { reasons.append("swap \(GuardText.human(swapUsed)) i rośnie") }
        level = reasons.isEmpty ? 0 : 1
        if !swapping && Double(swapUsed) >= warn { notes.append("swap \(GuardText.human(swapUsed)) stoi") }
        computeStage(cfg: cfg, kernel: kernelUsed)
    }

    mutating func computeStage(cfg: GuardConfig, kernel: Int) {
        (stage, stageReasons) = LastResort.stage(
            ram: ram, compressed: compressed, segments: segments, segmentsLimit: segmentsLimit, swapUsed: swapUsed,
            swapGrowth: swapGrowth, kernel: kernel, available: available, guardLevel: level, cfg: cfg)
    }

    public func summary() -> PyJSON {
        var o = PyObject()
        o["stage"] = .int(stage)
        o["stage_reasons"] = .array(stageReasons.map { .string($0) })
        o["segments"] = segments.map { .int($0) } ?? .null
        o["segments_limit"] = segmentsLimit.map { .int($0) } ?? .null
        o["level"] = .int(level)
        o["reasons"] = .array(reasons.map { .string($0) })
        o["notes"] = .array(notes.map { .string($0) })
        o["kernel"] = .int(kernel)
        o["available"] = available.map { .int($0) } ?? .null
        o["swap_used"] = .int(swapUsed)
        o["swap_total"] = .int(swapTotal)
        o["swap_growth"] = .int(swapGrowth)
        o["swapping"] = .bool(swapping)
        o["compressed"] = .int(compressed)
        o["ram"] = .int(ram)
        return .object(o)
    }
}

