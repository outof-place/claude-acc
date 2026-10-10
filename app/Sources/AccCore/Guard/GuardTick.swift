// One tick of devguard_core, without its actions: World(), check_pending, decide, the inventory,
// the history and the snapshot. When a plan or the memory brake would act, the tick says so and
// the caller hands that tick to Python (`acc.py devguard once`), which acts exactly as before.
import Darwin

public let MINUTE = 60.0
public let HOUR = 3600.0

// MARK: - world

public final class GuardWorld {
    public let now: Double
    public let rows: [ProcRow]
    public let table: ProcTable
    public var units: [Unit]
    public let simulators: [Simulator]
    public var pressure: Pressure
    public let orca: OrcaView?
    public let fseventsRestart: PyJSON
    public let home: String

    /// World(cfg, state, orca, now, use_orca)
    public init(cfg: GuardConfig, state: inout PyObject, orca: OrcaView, probes: GuardProbes, useOrca: Bool) {
        now = probes.now
        home = probes.home
        let rows = Profile.measure("processes") { probes.processes() }
        self.rows = rows
        let table = ProcTable(rows)
        self.table = table
        units = Profile.measure("discover") { discover(cfg, table, probes: probes) }
        let simulators = Profile.measure("simulators") { discoverSimulators(cfg, rows, probes: probes) }
        self.simulators = simulators
        trackSimulators(cfg, &state, simulators, now: now)
        var byUdid: [String: Simulator] = [:]
        for s in simulators { byUdid[s.udid] = s }
        var st = state
        let now = self.now
        pressure = Profile.measure("pressure") { Pressure(cfg: cfg, state: &st, now: now, probes: probes) }
        state = st
        if !units.isEmpty {
            let sockets = Profile.measure("sockets") { probes.sockets() }
            var owner: [Int: Unit] = [:]
            let listenPorts = Array(sockets.listen.keys)
            for unit in units {
                for server in unit.servers {
                    let tree = Set(server.tree)
                    server.ports = listenPorts.filter { !(sockets.listen[$0]!.isDisjoint(with: tree)) }.sorted()
                }
                let tree = Set(unit.pids)
                unit.ports = listenPorts.filter { !(sockets.listen[$0]!.isDisjoint(with: tree)) }.sorted()
                for port in unit.ports { owner[port] = unit }
            }
            for link in sockets.links {
                guard let unit = owner[link.port], !unit.pids.contains(link.pid) else { continue }
                let command = table[link.pid]?.command ?? "?"
                let facts = table[link.pid]?.facts ?? CommandFacts(command)
                let name = GuardText.prefix(basename(command.pyPartition(" -").0), 40)
                let entry = Unit.Client(pid: link.pid, kind: facts.clientKind, name: name)
                if !unit.clients.contains(entry) { unit.clients.append(entry) }
            }
            for unit in units { dropUnusedSimulatorClients(unit, table, byUdid) }
        }
        if useOrca && !units.isEmpty { Profile.measure("orca") { orca.refresh(rows, now: now, every: cfg.number("orca_seconds"), probes: probes) } }
        self.orca = orca.ok ? orca : nil
        let fsguard = probes.json(GuardPaths.fsguardState, fallback: .object(PyObject()))
        fseventsRestart = (fsguard.truthy ? fsguard : .object(PyObject()))["last_restart"] ?? .int(0)
        var protect: [String] = []
        var protectedPorts: Set<Int> = []
        for p in cfg["protect"].array ?? [] {
            if case .string(let s) = p, !s.pyStarts(":") { protect.append(Files.expand(s, home: home)) }
            let text = pyStr(p)
            let digits = String(String.UnicodeScalarView(text.unicodeScalars.drop(while: { $0 == ":" })))
            if !digits.isEmpty, digits.unicodeScalars.allSatisfy(pyIsDigit), let n = Int(digits) { protectedPorts.insert(n) }
        }
        let pins = loadPins(now: now, probes: probes)
        var history = state["units"]?.object ?? PyObject()
        var seen: Set<String> = []
        for unit in units {
            if let orca = self.orca {
                unit.worktree = orca.worktree(for: unit.launchCwd)
                unit.terminal = orca.terminal(for: unit.ancestors)
                for tab in orca.tabs {
                    if let port = tab["port"]?.int, unit.ports.contains(port) {
                        var t = tab
                        t["focused"] = .bool(orca.focused(tab))
                        unit.tabs.append(t)
                    }
                }
                let consumerIds = unit.tabs.map { $0["worktreeId"] ?? .null }
                unit.consumers = orca.worktrees.filter { w in consumerIds.contains(w["worktreeId"] ?? .null) }
            }
            unit.protected = !Set(unit.ports).isDisjoint(with: protectedPorts)
                || unit.servers.contains { s in protect.contains { s.cwd == $0 || s.cwd.pyStarts($0 + "/") } }
            unit.pin = pins.first { pinMatches($0, unit) }
            if unit.pin != nil { unit.protected = true }
            seen.insert(unit.key)
            var h = history[unit.key]?.object ?? PyObject([
                ("first", .double(now)), ("cpu", unit.cpu.json), ("at", .double(now)), ("busy", .double(now)), ("watched", .double(now)),
            ])
            let at = h["at"]?.double ?? now
            let dt = max(now - at, 0.001)
            if (unit.cpu.value - (h["cpu"]?.double ?? 0)) / dt >= 0.05 { h["busy"] = .double(now) }
            if unit.watched { h["watched"] = .double(now) }
            h["cpu"] = unit.cpu.json
            h["at"] = .double(now)
            let output = unit.terminal?["lastOutputAt"]
            let outputS = (output?.truthy ?? false) ? (output?.double ?? 0) / 1000 : 0
            let busy = max(h["busy"]?.double ?? 0, outputS)
            let first = h["first"]?.double ?? now
            unit.age = now - first
            unit.started = startedAt(unit.start, first: first, now: now, absolute: probes.absolute)
            unit.quiet = max(0.0, now - busy)
            unit.lastWatched = PyNum(h["watched"]) ?? .double(now)
            history[unit.key] = .object(h)
        }
        for key in history.keys where !seen.contains(key) { history[key] = nil }
        state["units"] = .object(history)
    }
}

/// str(x) for the values a config list can hold
func pyStr(_ v: PyJSON) -> String {
    switch v {
    case .string(let s): s
    case .int(let i): String(i)
    case .double(let d): PyJSON.repr(d)
    case .bool(let b): b ? "True" : "False"
    case .null: "None"
    default: v.dumps()
    }
}

public enum GuardPaths {
    nonisolated(unsafe) public static var fsguardState = "/var/db/claude-acc-fsguard.json"
}

func startedAt(_ abstime: UInt64, first: Double, now: Double, absolute: UInt64) -> Double {
    guard abstime != 0 else { return first }
    let awake = Double(absolute &- abstime) * Kernel.nsPerTick / 1e9
    return min(first, now - awake)
}

func clientKind(_ command: String) -> String {
    if GuardPatterns.simDevice.search(command) { return "simulator" }
    if GuardContext.host.apps.search(command) { return "orca" }
    if GuardPatterns.headless.search(command) { return "headless" }
    if GuardPatterns.browser.search(command) { return "browser" }
    return "tool"
}

func dropUnusedSimulatorClients(_ unit: Unit, _ table: ProcTable, _ sims: [String: Simulator]) {
    var kept: [Unit.Client] = []
    var idle: [Unit.Client] = []
    for client in unit.clients {
        let found = client.kind == "simulator" ? table.facts(client.pid).simDevice : nil
        let sim = found.flatMap { sims[$0] }
        if let sim, !sim.inUse { idle.append(client) } else { kept.append(client) }
    }
    unit.clients = kept
    unit.idleSimClients = idle
}

func trackSimulators(_ cfg: GuardConfig, _ state: inout PyObject, _ sims: [Simulator], now: Double) {
    var history = state["sims"]?.object ?? PyObject()
    let busyCores = cfg.number("simulator_busy_cores")
    var seen: Set<String> = []
    for sim in sims {
        let key = "\(sim.udid):\(sim.start)"
        seen.insert(key)
        var h = history[key]?.object ?? PyObject([("first", .double(now)), ("cpu", sim.cpu.json), ("at", .double(now)), ("busy", .double(now))])
        let dt = max(now - (h["at"]?.double ?? now), 0.001)
        if (sim.cpu.value - (h["cpu"]?.double ?? 0)) / dt >= busyCores || sim.leaseAlive || sim.watched { h["busy"] = .double(now) }
        h["cpu"] = sim.cpu.json
        h["at"] = .double(now)
        sim.age = now - (h["first"]?.double ?? now)
        sim.quiet = now - (h["busy"]?.double ?? now)
        history[key] = .object(h)
    }
    for key in history.keys where !seen.contains(key) { history[key] = nil }
    state["sims"] = .object(history)
}

/// load_pins: the pins with a target that haven't expired
func loadPins(now: Double, probes: GuardProbes) -> [PyObject] {
    let file = probes.json(GuardPaths.pins(probes.home), fallback: .object(PyObject()))
    let pins = file["pins"]?.array ?? []
    return pins.compactMap(\.object).filter { p in
        guard p["target"]?.truthy ?? false else { return false }
        let until = p["until"] ?? .null
        return until.isNull || (until.double.map { $0 > now } ?? false)
    }
}

func pinMatches(_ pin: PyObject, _ unit: Unit) -> Bool {
    let target = pin["target"]?.string ?? ""
    if target.pyStarts(":") {
        let digits = String(String.UnicodeScalarView(target.unicodeScalars.dropFirst()))
        guard !digits.isEmpty, digits.unicodeScalars.allSatisfy(pyIsDigit), let n = Int(digits) else { return false }
        return unit.ports.contains(n)
    }
    return unit.servers.contains { $0.cwd == target || $0.cwd.pyStarts(target + "/") }
}

extension GuardPaths {
    public static func state(_ home: String) -> String { home + "/.local/share/claude-acc" }
    public static func pins(_ home: String) -> String { state(home) + "/devguard-pins.json" }
}

// MARK: - plans

public final class Plan {
    public enum Target {
        case unit(Unit)
        case simulator(Simulator)
        case simulators(footprint: Int, count: Int)
    }

    public let target: Target
    public var action: String
    public let priority: Int
    public var reason: String
    public var code: String
    public var data: PyObject

    init(_ target: Target, _ action: String, _ priority: Int, _ reason: String, _ code: String, _ data: [(String, PyJSON)] = []) {
        self.target = target
        self.action = action
        self.priority = priority
        self.reason = reason
        self.code = code
        self.data = PyObject(data)
    }

    public var key: String {
        switch target {
        case .unit(let u): u.key
        case .simulator(let s): s.key
        case .simulators: "simulators"
        }
    }

    public var appKey: String {
        switch target {
        case .unit(let u): u.appKey
        case .simulator(let s): s.key
        case .simulators: "simulators"
        }
    }

    public func label(home: String) -> String {
        switch target {
        case .unit(let u): u.label(home: home)
        case .simulator(let s): s.label
        case .simulators(_, let count): "\(count) włączone symulatory"
        }
    }

    var attended: Bool { if case .unit(let u) = target { u.attended } else { false } }
    var pinned: Bool { if case .unit(let u) = target { u.pin != nil } else { false } }

    public func summary(home: String) -> PyJSON {
        .object(PyObject([
            ("unit", .string(key)), ("label", .string(label(home: home))), ("action", .string(action)), ("reason", .string(reason)),
            ("code", .string(code)), ("data", .object(data)),
        ]))
    }
}

/// max(items, key=...) : the first maximal item
func pyMax<T, K: Comparable>(_ items: [T], by key: (T) -> K) -> T? {
    var best: T?
    var bestKey: K?
    for item in items {
        let k = key(item)
        if bestKey == nil || k > bestKey! {
            best = item
            bestKey = k
        }
    }
    return best
}

/// A tuple key for max(): Python compares bools and numbers in order
struct Key4: Comparable {
    var a: Bool, b: Bool, c: Double, d: Double
    static func < (l: Key4, r: Key4) -> Bool {
        if l.a != r.a { return !l.a }
        if l.b != r.b { return !l.b }
        if l.c != r.c { return l.c < r.c }
        return l.d < r.d
    }
}

struct Key2: Comparable {
    var a: Double, b: Double
    static func < (l: Key2, r: Key2) -> Bool { l.a != r.a ? l.a < r.a : l.b < r.b }
}

func cfgText(_ v: PyJSON) -> String { pyStr(v) }

/// cfg[key] * factor with Python's types (an int config stays an int)
func scaled(_ v: PyJSON, _ factor: Int) -> PyNum {
    switch v {
    // an int Python would grow past Int64 (a "limit off" of 10**12 GB): a float here, not a trap
    case .int(let i): i.multipliedReportingOverflow(by: factor).overflow ? .double(Double(i) * Double(factor)) : .int(i * factor)
    case .bool(let b): .int((b ? 1 : 0) * factor)
    default: .double((v.double ?? 0) * Double(factor))
    }
}

public func decide(_ cfg: GuardConfig, _ world: GuardWorld, _ state: PyObject) -> [Plan] {
    let now = world.now
    let pressure = world.pressure
    let units = world.units
    let home = world.home
    let grace = cfg.number("grace_minutes") * MINUTE
    let maxFp = scaled(cfg["max_server_gb"], GuardText.GB)
    let mature = units.filter { !$0.protected && $0.age >= grace }
    let isMature = { (u: Unit) in mature.contains { $0 === u } }
    var plans: [Plan] = []

    var byApp: [(String, [Unit])] = []
    var appIndex: [String: Int] = [:]
    for unit in units {
        for server in unit.servers {
            if let i = appIndex[server.cwd] {
                byApp[i].1.append(unit)
            } else {
                appIndex[server.cwd] = byApp.count
                byApp.append((server.cwd, [unit]))
            }
        }
    }
    for (app, list) in byApp {
        var group: [Unit] = []
        for u in list where !group.contains(where: { $0.key == u.key }) { group.append(u) }
        if group.count < 2 { continue }
        let keep = pyMax(group) { Key4(a: $0.attended, b: $0.watched, c: $0.lastWatched.value, d: Double($0.start)) }!
        for unit in group {
            if unit === keep || !isMature(unit) || unit.watched { continue }
            if now - unit.lastWatched.value >= cfg.number("duplicate_minutes") * MINUTE {
                plans.append(Plan(
                    .unit(unit), "stop", 60, "drugi serwer \(GuardText.short(app, home: home)), zostaje \(keep.label(home: home))", "duplicate",
                    [("keep", keep.ports.first.map { .int($0) } ?? .null)]))
            }
        }
    }

    for unit in mature {
        if unit.host == "orphan" && !unit.watched && now - unit.lastWatched.value >= cfg.number("orphan_minutes") * MINUTE {
            plans.append(Plan(.unit(unit), "stop", 50, "agent albo terminal, który go postawił, już nie żyje", "orphan"))
        }
        let idle = cfg.number("idle_minutes") * MINUTE * (unit.agentWorking ? 2 : 1)
        if !unit.watched && unit.quiet >= idle && now - unit.lastWatched.value >= idle {
            plans.append(Plan(
                .unit(unit), "stop", 40, "nikt go nie ogląda i nic nie robi od \(GuardText.minutes(unit.quiet))", "idle",
                [("minutes", .int(GuardText.int((unit.quiet / MINUTE).rounded(.down))))]))
        }
        if Double(unit.biggest) >= maxFp.value {
            let needed = cfg.number("quiet_seconds") * (unit.attended ? 10 : 1)
            let why = "spuchł do \(GuardText.human(unit.biggest)) (limit \(cfgText(cfg["max_server_gb"])) GB)"
            if unit.quiet < needed { continue }
            let size: [(String, PyJSON)] = [("size", .int(unit.biggest)), ("limit", maxFp.json)]
            if unit.recyclable {
                plans.append(Plan(.unit(unit), "recycle", 70, why, "bloated", size))
            } else if !unit.watched {
                plans.append(Plan(.unit(unit), "stop", 70, why, "bloated", size))
            } else {
                plans.append(Plan(.unit(unit), "warn", 10, why + "; nie mam jak go zrestartować", "bloated_unmanaged", size))
            }
        }
    }

    for unit in units {
        guard let pin = unit.pin, unit.age >= grace, Double(unit.biggest) >= maxFp.value else { continue }
        if pin["level"] == .string("hold") || !unit.recyclable { continue }
        if unit.quiet < cfg.number("quiet_seconds") * (unit.attended ? 10 : 1) { continue }
        plans.append(Plan(
            .unit(unit), "recycle", 70,
            "spuchł do \(GuardText.human(unit.biggest)) (limit \(cfgText(cfg["max_server_gb"])) GB); przypięty, więc restart zamiast zatrzymania",
            "bloated", [("size", .int(unit.biggest)), ("limit", maxFp.json)]))
    }

    let total = units.reduce(0) { $0 + $1.footprint }
    let budget = cfg.number("budget_percent") / 100 * Double(pressure.ram)
    let idlePool = cfg.number("idle_minutes") * MINUTE / 3
    if pressure.level != 0 || Double(total) > budget {
        var pool: [Unit] = []
        for unit in mature {
            let bloated = Double(unit.biggest) >= maxFp.value
            if pressure.level == 2 {
                if unit.attended && !unit.recyclable { continue }
            } else if unit.attended || unit.quiet < cfg.number("quiet_seconds") {
                continue
            } else if pressure.level == 1 {
                if unit.watched && !bloated { continue }
            } else if !bloated && (unit.watched || unit.quiet < idlePool) {
                continue
            }
            pool.append(unit)
        }
        func score(_ u: Unit) -> Double {
            let q = u.quiet / 1800
            return Double(u.footprint) * (1 + (q < 2 ? q : 2)) * (u.watched ? 0.5 : 1) * (u.agentWorking ? 0.7 : 1)
        }
        if let top = pyMax(pool, by: score) {
            let why = pressure.level != 0
                ? "brak pamięci: " + pressure.reasons.joined(separator: ", ")
                : "dev serwery zajmują \(GuardText.human(total)), budżet \(GuardText.human(budget))"
            let action = top.recyclable && (top.watched || top.attended) ? "recycle" : "stop"
            let (code, data): (String, [(String, PyJSON)]) = pressure.level != 0
                ? ("pressure", [("level", .int(pressure.level))])
                : ("budget", [("total", .int(total)), ("budget", .double(budget))])
            plans.append(Plan(.unit(top), action, pressure.level == 2 ? 90 : 80, why, code, data))
        } else if pressure.level == 2 {
            let pinned = units.filter { $0.pin != nil && $0.recyclable && $0.age >= grace }
            if let top = pyMax(pinned, by: { $0.footprint }) {
                plans.append(Plan(
                    .unit(top), "recycle", 90,
                    "brak pamięci: " + pressure.reasons.joined(separator: ", ") + "; przypięty, więc restart zamiast zatrzymania", "pressure",
                    [("level", .int(2))]))
            }
        }
    }

    let restart = world.fseventsRestart
    for unit in units {
        let allowed = unit.pin.map { $0["level"] != .string("hold") } ?? !unit.protected
        if restart.truthy, unit.recyclable, allowed, let r = restart.double, unit.started < r {
            plans.append(Plan(.unit(unit), "recycle", 95, "po restarcie fseventsd nie widzi zmian plików", "fsevents"))
        }
    }

    plans += simulatorPlans(cfg, world.simulators, pressure, grace: grace)

    for plan in plans where plan.action == "recycle" {
        let count = recentRecycles(state, plan.appKey, now)
        if Double(count) >= cfg.number("max_recycles_per_hour") {
            plan.data["restarts"] = .int(count)
            if plan.attended || plan.pinned {
                plan.action = "warn"
                plan.code = "loop_watched"
                let why = plan.attended ? "go oglądasz" : "jest przypięty"
                plan.reason += "; \(count) restarty w godzinę, nie ruszam, bo \(why)"
            } else {
                plan.action = "stop"
                plan.code = "loop"
                plan.reason += "; \(count) restarty w godzinę to pętla, zatrzymuję"
            }
        }
    }

    var best: [Plan] = []
    var keys: Set<String> = []
    for plan in stableSorted(plans, by: { -$0.priority }) where keys.insert(plan.key).inserted { best.append(plan) }
    return stableSorted(best, by: { -$0.priority })
}

func stableSorted<T>(_ items: [T], by key: (T) -> Int) -> [T] {
    items.enumerated().sorted { l, r in
        let a = key(l.element), b = key(r.element)
        return a != b ? a < b : l.offset < r.offset
    }.map(\.element)
}

func recentRecycles(_ state: PyObject, _ appKey: String, _ now: Double) -> Int {
    (state["recycles"]?.array ?? []).filter { e in
        guard let a = e.array, a.count > 1 else { return false }
        return a[1] == .string(appKey) && now - (a[0].double ?? 0) < HOUR
    }.count
}

func simulatorPlans(_ cfg: GuardConfig, _ sims: [Simulator], _ pressure: Pressure, grace: Double) -> [Plan] {
    if sims.isEmpty { return [] }
    let cap = cfg["max_booted_simulators"]
    let idle = cfg.number("simulator_idle_minutes") * MINUTE
    let safe = sims.filter { !$0.inUse && !$0.protected && $0.age >= grace }
    var plans = safe.filter { $0.quiet >= idle }.map { s in
        Plan(.simulator(s), "shutdown", 45, "nikt go nie używa od \(GuardText.minutes(s.quiet))", "simulator_idle",
             [("minutes", .int(GuardText.int((s.quiet / MINUTE).rounded(.down))))])
    }
    let over = cap.truthy && Double(sims.count) > (cap.double ?? 0)
    if !(over || pressure.level != 0) { return plans }
    let ready = safe.filter { $0.quiet >= cfg.number("simulator_quiet_minutes") * MINUTE }
    if let top = pyMax(ready, by: { Key2(a: $0.quiet, b: Double($0.footprint)) }) {
        let unused = "nieużywany od \(GuardText.minutes(top.quiet))"
        var data: [(String, PyJSON)] = [("minutes", .int(GuardText.int((top.quiet / MINUTE).rounded(.down))))]
        let why: String
        let code: String
        if over {
            why = "\(sims.count) włączone symulatory, limit \(cfgText(cap)); ten \(unused)"
            code = "simulator_cap"
            data += [("booted", .int(sims.count)), ("cap", cap)]
        } else {
            why = "brak pamięci: " + pressure.reasons.joined(separator: ", ") + "; symulator \(unused)"
            code = "pressure"
            data += [("level", .int(pressure.level))]
        }
        plans.append(Plan(.simulator(top), "shutdown", 85, why, code, data))
    } else if over {
        let footprint = sims.reduce(0) { $0 + $1.footprint }
        plans.append(Plan(
            .simulators(footprint: footprint, count: sims.count), "warn", 10,
            "\(sims.count) włączone symulatory (\(GuardText.human(footprint))), limit \(cfgText(cap)); każdy jest w użyciu albo dopiero wstał",
            "simulator_cap", [("booted", .int(sims.count)), ("cap", cap)]))
    }
    return plans
}

// MARK: - inventory

public func inventory(_ world: GuardWorld, probes: GuardProbes) -> PyJSON {
    var dev: Set<pid_t> = []
    for u in world.units { dev.formUnion(u.pids) }
    var jobs: Set<pid_t> = []
    let sched = probes.json(GuardPaths.state(probes.home) + "/sched/state.json", fallback: .object(PyObject()))
    let running = (sched.object?["running"]).flatMap { $0.truthy ? $0.array : nil } ?? []
    if !running.isEmpty {
        let children = world.table.children()
        for job in running {
            if let pgid = job["child_pgid"], pgid.truthy, let p = pgid.int { jobs.formUnion(descendants(pid_t(clamping: p), children)) }
        }
    }
    var families = PyObject()
    var biggest: (Int, Int, String) = (0, 0, "")
    var tops: [String: (Int, Int, String)] = [:]
    var counts: [String: (Int, Int)] = [:]
    var order: [String] = []
    for pid in world.table.order {
        let command = world.table.command(pid)
        let facts = world.table.facts(pid)
        guard let info = probes.usage(pid), info.footprint != 0 else { continue }
        let size = Int(info.footprint)
        let name: String
        if dev.contains(pid) {
            name = "dev"
        } else if jobs.contains(pid) {
            name = "jobs"
        } else {
            name = facts.family
        }
        if counts[name] == nil {
            order.append(name)
            counts[name] = (0, 0)
            tops[name] = (0, 0, "")
        }
        counts[name]!.0 += 1
        counts[name]!.1 += size
        if size > tops[name]!.1 { tops[name] = (Int(pid), size, GuardText.prefix(command, 120)) }
        if size > biggest.1 && !facts.sacred { biggest = (Int(pid), size, GuardText.prefix(command, 120)) }
    }
    var longLived = 0
    for name in order {
        let (count, footprint) = counts[name]!
        let top = tops[name]!
        families[name] = .object(PyObject([
            ("count", .int(count)), ("footprint", .int(footprint)), ("top", .array([.int(top.0), .int(top.1), .string(top.2)])),
        ]))
        if GuardPatterns.longLived.contains(name) { longLived += footprint }
    }
    return .object(PyObject([
        ("at", .double(world.now)), ("families", .object(families)), ("long_lived", .int(longLived)),
        ("biggest", .array([.int(biggest.0), .int(biggest.1), .string(biggest.2)])),
    ]))
}

// MARK: - tick

/// What a tick would hand to Python's actions. Python's loop gives every plan to execute() in order
/// until one acts, then brakes if none did; `actable` is the plans it would try (enforcing, past the
/// cooldown, a warn at most hourly) and `brakeDue` whether brake() would look for a victim with
/// nothing acted. Which of them goes to `acc.py devguard once` is GuardEngine's call (HandoverPolicy).
public struct GuardTickResult {
    public var world: GuardWorld
    public var plans: [Plan]
    public var actable: [Plan]
    public var brakeDue: Bool
}

/// tick(cfg, state, orca, qos) up to the actions: the state is updated the way Python's tick updates
/// it when nothing acts. With `shaper` (the resident loop, enforcing) the servers you don't look at
/// go to background QoS before the snapshot records it, as shape() does in Python's loop.
public func guardTick(
    cfg: GuardConfig, state: inout PyObject, orca: OrcaView, probes: GuardProbes, enforce: Bool, shaper: QoSShaper? = nil
) -> GuardTickResult {
    let now = probes.now
    let previous = state["snapshot"]?["pressure"]?["stage"]?.int ?? 0
    let world = GuardWorld(cfg: cfg, state: &state, orca: orca, probes: probes, useOrca: previous < 3)
    checkPending(world, &state, probes: probes)
    let plans = Profile.measure("decide") { decide(cfg, world, state) }
    var actable: [Plan] = []
    if !plans.isEmpty && enforce && now - (state["last_action"]?.double ?? 0) >= cfg.number("cooldown_seconds") {
        actable = plans.filter { wouldAct($0, state: state, now: now) }
    }
    let stage = world.pressure.stage
    let inv = state["inventory"]?.object
    if !world.table.order.isEmpty && (stage >= 1 || now - (inv?["at"]?.double ?? 0) >= 30) {
        state["inventory"] = Profile.measure("inventory") { inventory(world, probes: probes) }
    }
    let brake = enforce && brakeDue(cfg, world, state, now: now)
    var history = state["history"]?.array ?? []
    if history.isEmpty || now - (history.last?.array?.first?.double ?? 0) >= 30 {
        let p = world.pressure
        history.append(.array([.int(GuardText.round(now)), .int(world.units.reduce(0) { $0 + $1.footprint }), .int(p.swapUsed), .int(p.compressed)]))
        if history.count > 240 { history.removeFirst(history.count - 240) }
    }
    state["history"] = .array(history)
    if enforce, let shaper { shaper.shape(cfg, world, probes: probes) }
    state["snapshot"] = Profile.measure("snapshot") { snapshot(cfg, world, plans, state: state) }
    return GuardTickResult(world: world, plans: plans, actable: actable, brakeDue: brake)
}

/// Whether execute() would do something rather than return False: a warn repeats at most hourly.
/// Holds that depend on the screen or a lease (Simulator in front, a session taking a simulator) are
/// Python's to judge (`declinable`); the caller backs off after a handover that changed nothing.
func wouldAct(_ plan: Plan, state: PyObject, now: Double) -> Bool {
    if plan.action == "warn" {
        let warned = state["warned"]?[plan.appKey]?.double ?? 0
        return now - warned >= HOUR
    }
    return true
}

/// Whether Python's execute() may return False for the plan on its own reading: a pool shutdown
/// (shutdown_simulator checks leases and the screen) and a server with an app open in a Simulator
/// while Simulator is in front. Every other plan acts once it reaches execute().
func declinable(_ plan: Plan) -> Bool {
    if plan.action == "shutdown" { return true }
    if plan.action == "warn" { return false }
    if case .unit(let unit) = plan.target { return !unit.idleSimClients.isEmpty }
    return false
}

/// brake(): whether lastresort.reap would be asked for a victim this tick, with no plan acted.
func brakeDue(_ cfg: GuardConfig, _ world: GuardWorld, _ state: PyObject, now: Double) -> Bool {
    guard cfg.raw["last_resort"].map({ $0.truthy }) ?? true else { return false }
    let p = world.pressure
    let level = p.stage
    let s = LastResort.settings(cfg)
    let inv = state["inventory"]?.object
    let big = inv?["biggest"].flatMap { $0.truthy ? $0.array : nil } ?? [.int(0), .int(0)]
    let limit = (s["runaway_percent"]?.double ?? 50) / 100 * Double(p.ram)
    let runaway = big.count > 1 && (big[1].double ?? 0) >= limit && limit > 0
    if level < 2 && !runaway { return false }
    let gap = (level >= 3 ? s["emergency_cooldown_seconds"] : s["brake_cooldown_seconds"])?.double ?? 0
    if now - (state["brake_at"]?.double ?? 0) < gap { return false }
    if level < 3 && !runaway && now - (state["last_action"]?.double ?? 0) < cfg.number("cooldown_seconds") { return false }
    return true
}

func checkPending(_ world: GuardWorld, _ state: inout PyObject, probes: GuardProbes) {
    var pending: [PyJSON] = []
    let apps = Set(world.units.map(\.appKey))
    for entry in state["pending"]?.array ?? [] {
        let app = entry["app"]?.string ?? ""
        let at = entry["at"]?.double ?? 0
        let label = pyStr(entry["label"] ?? .null)
        if apps.contains(app) {
            GuardLog.write("wstał po restarcie \(label) w \(GuardText.round(world.now - at)) s")
        } else if world.now - at > 60 {
            let command = pyStr(entry["command"] ?? .null)
            GuardLog.write("nie wstał po restarcie \(label); komenda: \(command)")
            GuardLog.notify("Dev serwer nie wstał", "\(label): \(command)")
        } else {
            pending.append(entry)
        }
    }
    state["pending"] = .array(pending)
}

func snapshot(_ cfg: GuardConfig, _ world: GuardWorld, _ plans: [Plan], state: PyObject) -> PyJSON {
    let home = world.home
    let units = stableSortedDouble(world.units) { -Double($0.footprint) }
    return .object(PyObject([
        ("at", .double(world.now)),
        ("mode", cfg["mode"]),
        ("pressure", world.pressure.summary()),
        ("budget", .double(cfg.number("budget_percent") / 100 * Double(world.pressure.ram))),
        ("total", .int(world.units.reduce(0) { $0 + $1.footprint })),
        ("orca", .bool(world.orca != nil)),
        ("units", .array(units.map { $0.summary(home: home) })),
        ("simulators", .array(world.simulators.map { $0.summary() })),
        ("simulator_cap", cfg["max_booted_simulators"]),
        ("plans", .array(plans.map { $0.summary(home: home) })),
        ("acted", .null),
        ("last_resort", .null),
        ("inventory", state["inventory"] ?? .null),
    ]))
}

func stableSortedDouble<T>(_ items: [T], by key: (T) -> Double) -> [T] {
    items.enumerated().sorted { l, r in
        let a = key(l.element), b = key(r.element)
        return a != b ? a < b : l.offset < r.offset
    }.map(\.element)
}
