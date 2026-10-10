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
// Shadow mode runs nothing. It watches launchd's own ticks and records, for each, whether this
// schedule would have run it and whether it changed anything (docs/acc-cored.md).
import Darwin
import Dispatch

public final class SwitchEngine: @unchecked Sendable {
    public struct Options: Sendable {
        public var home: String
        public var python: [String]
        /// observe launchd's ticks instead of running them; the observations go to this file
        public var shadowPath: String?
        public var slot = 120.0
        public var maxGap = 480.0

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
    /// the host app's data directory and settings file name (orcahost.py), read once
    var host: (userData: String, dataFile: String)?
    /// the inputs' stamps when the last tick ended
    var baseline: [String: String] = [:]
    public private(set) var runs = 0
    public private(set) var skips = 0
    /// tests: the tick's run in place of Python's
    var runner: (([String]) -> Int32)?

    public init(_ options: Options) {
        self.options = options
    }

    // MARK: decisions

    /// What a tick reads and others may write, as stamps (inode, size, mtime). A missing file has
    /// none; the account list is one stamp of its names and their oauth-account.json files.
    func stamps() -> [String: String] {
        var out: [String: String] = [:]
        func add(_ name: String, _ path: String) { out[name] = Files.stamp(path).map { "\($0)" } ?? "-" }
        add("keychain", options.home + "/Library/Keychains/login.keychain-db")
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

    /// Whether the slot at `now` runs the tick: the inputs that changed since the baseline, the
    /// last tick's `tick_due`, the gap since it.
    static func decide(state: PyObject, changed: [String], now: Double, maxGap: Double) -> Decision {
        var reasons = changed.map { "changed: " + $0 }
        let lastTick = state["last_tick"]?.double ?? 0
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
            self.host = HostResolver.dataFiles(python: self.options.python, home: self.options.home)
            self.baseline = self.stamps()
            if self.options.shadowPath != nil {
                self.startShadow()
            } else {
                self.scheduleSlot()
            }
        }
    }

    public func stop() {
        queue.sync { timer?.cancel() }
    }

    /// The next slot: `slot` after the last tick's start (launchd's cadence), at least a second away.
    func scheduleSlot() {
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        let lastTick = state["last_tick"]?.double ?? 0
        let now = Kernel.wall()
        var next = lastTick + options.slot
        while next < now + 1 { next += options.slot }
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(wallDeadline: .now() + (next - now), leeway: .seconds(2))
        t.setEventHandler { [weak self] in self?.slotFired() }
        t.resume()
        timer = t
    }

    func slotFired() {
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        let decision = Self.decide(state: state, changed: changedInputs(stamps()), now: Kernel.wall(), maxGap: options.maxGap)
        if decision.run {
            let argv = options.python + ["accswitch", "tick"]
            _ = runner?(argv) ?? Spawn.run(argv, log: nil, timeout: 110)
            runs += 1
            baseline = stamps()
        } else {
            skips += 1
        }
        scheduleSlot()
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
        let state = Files.json(stateDir + "/state.json", fallback: .null).object ?? PyObject()
        let tick = state["last_tick"]?.double ?? 0
        if tick > lastSeenTick {
            // a tick started (its first write is last_tick): decide as the slot would have
            lastSeenTick = tick
            let now = stamps()
            var changed = changedInputs(now).filter { $0 != "state.json" }
            if Self.comparable(state) != Self.comparable(previousState) { changed.append("state.json") }
            var decisionState = previousState
            if decisionState["tick_due"] == nil { decisionState["tick_due"] = estimatedDue(previousState).map { .object($0) } }
            let decision = Self.decide(state: decisionState, changed: changed, now: tick, maxGap: options.maxGap)
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
        guard let email = status["active_email"]?.string, !(status["pause"]?.truthy ?? false),
              let hit = cache[email]?.object, let ts = hit["ts"]?.double, let data = hit["data"]?.object
        else { return PyObject([("quiet", false), ("at", .null), ("why", "")]) }
        let hardS = cfg["hard_session_left"]?.double ?? 5
        let hardW = cfg["hard_weekly_left"]?.double ?? 3
        let (session, weekly) = Self.left(data, now: Kernel.wall())
        guard session > hardS, weekly > hardW else { return PyObject([("quiet", false), ("at", .null), ("why", "")]) }
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
    /// orcahost.py's user_data and data_file (a cold path: once at start)
    public static func dataFiles(python: [String], home: String) -> (userData: String, dataFile: String)? {
        let script = GuardPaths.state(home) + "/orcahost.py"
        let argv = python.filter { !$0.hasSuffix("/acc.py") } + [script]
        guard let out = Spawn.output(argv, timeout: 20), let host = (try? PyJSON.loads(bytes: out))?.object,
              let userData = host["user_data"]?.string, let dataFile = host["data_file"]?.string
        else { return nil }
        return (userData, dataFile)
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
        let parts = rest.dropFirst().split(separator: ":")
        guard parts.count == 2, let h = Double(parts[0]), let m = Double(parts[1]) else { return nil }
        return seconds - sign * (h * 3600 + m * 60)
    }
}
