// devguard_core.Orca: the host's preview tabs, worktrees, agents and terminals, refreshed every
// `orca_seconds` while dev servers run. The reads go over the socket the host's CLI uses
// (orca-runtime.json), and only when that fails through the CLI itself, as the Python guard does.
import Darwin

/// What the guard keeps of the host between ticks (Python's Orca instance).
public final class OrcaView {
    public var ok = false
    public var at = 0.0
    public var sessionsAt = 0.0
    public var tabs: [PyObject] = []
    public var worktrees: [PyObject] = []
    public var terminals: [PyObject] = []
    /// pid of a terminal's process (login) -> ptyId
    public var sessions: [Int: PyJSON] = [:]

    public init() {}

    /// Orca.refresh(rows, now, every, sessions_every=60)
    public func refresh(_ rows: [ProcRow], now: Double, every: Double, probes: GuardProbes, sessionsEvery: Double = 60) {
        guard let marker = probes.orcaMarker, rows.contains(where: { $0.command.pyContains(marker) }) else {
            ok = false
            return
        }
        // also after a failed read: a host that doesn't answer doesn't get 3 calls every 5 s
        if at != 0, now - at < every { return }
        let tabs = probes.orca(.tabs)
        let worktrees = probes.orca(.worktrees)
        let terminals = probes.orca(.terminals)
        ok = tabs != nil && worktrees != nil
        at = now
        self.tabs = Self.list(tabs, "tabs").map { t in
            var t = t
            t["port"] = portOf(t["url"]?.string ?? "").map { .int($0) } ?? .null
            return t
        }
        self.worktrees = Self.list(worktrees, "worktrees")
        self.terminals = Self.list(terminals, "terminals")
        if now - sessionsAt >= sessionsEvery {
            loadSessions(probes)
            sessionsAt = now
        }
    }

    /// (x or {}).get(key, []) as a list of objects
    static func list(_ value: PyJSON?, _ key: String) -> [PyObject] {
        guard let value, value.truthy, let items = value[key]?.array else { return [] }
        return items.compactMap(\.object)
    }

    func loadSessions(_ probes: GuardProbes) {
        let memory = probes.orca(.memory)
        var out: [Int: PyJSON] = [:]
        for w in Self.list(memory, "worktrees") {
            for s in (w["sessions"]?.array ?? []).compactMap(\.object) {
                guard let pid = s["pid"], pid.truthy, let key = pid.int else { continue }
                out[key] = s["sessionId"] ?? .null
            }
        }
        sessions = out
    }

    /// The worktree a path lies in (the longest match).
    public func worktree(for path: String) -> PyObject? {
        var best: PyObject?
        for w in worktrees {
            let root = (w["path"]?.truthy ?? false) ? (w["path"]?.string ?? "") : ""
            guard !root.isEmpty else { continue }
            var trimmed = root
            while trimmed.hasSuffix("/") { trimmed.removeLast() }
            if path == root || path.pyStarts(trimmed + "/") {
                if best == nil || root.unicodeScalars.count > (best!["path"]?.string ?? "").unicodeScalars.count { best = w }
            }
        }
        return best
    }

    public func worktree(id: PyJSON?) -> PyObject? {
        worktrees.first { $0["worktreeId"] == (id ?? .null) }
    }

    /// The host terminal a process with these ancestors runs in.
    public func terminal(for ancestors: [pid_t]) -> PyObject? {
        for pid in ancestors {
            if let pty = sessions[Int(pid)], pty.truthy {
                return terminals.first { $0["ptyId"] == pty }
            }
        }
        return nil
    }

    /// The tab you look at: the active one in the worktree selected in the host.
    public func focused(_ tab: PyObject) -> Bool {
        let w = worktree(id: tab["worktreeId"])
        return (tab["active"]?.truthy ?? false) && !(w?.isEmpty ?? true) && (w?["isActive"]?.truthy ?? false)
    }
}

/// devguard_core.port_of: the port of a loopback URL (80/443 by scheme), nil for anything else.
public func portOf(_ url: String) -> Int? {
    guard let parts = URLParts(url) else { return nil }
    let host = parts.hostname ?? ""
    guard ["localhost", "127.0.0.1", "::1", "0.0.0.0"].contains(host) || host.pyEnds(".localhost") else { return nil }
    guard let port = parts.port else { return nil }  // a port Python can't read raises ValueError: None
    if let p = port, p != 0 { return p }
    return parts.scheme == "https" ? 443 : 80
}

/// urllib.parse.urlsplit, the parts port_of reads.
struct URLParts {
    var scheme = ""
    var netloc = ""

    init?(_ raw: String) {
        let c0: (Unicode.Scalar) -> Bool = { $0.value <= 0x20 }
        var url = String(String.UnicodeScalarView(raw.unicodeScalars.drop(while: c0)))
        url = url.pyReplace("\t", "").pyReplace("\r", "").pyReplace("\n", "")
        let scalars = Array(url.unicodeScalars)
        if let i = scalars.firstIndex(of: ":"), i > 0, scalars[0].isASCII, Character(scalars[0]).isLetter {
            let schemeChars = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.".unicodeScalars)
            if scalars[..<i].allSatisfy(schemeChars.contains) {
                scheme = String(String.UnicodeScalarView(scalars[..<i])).lowercased()
                url = String(String.UnicodeScalarView(scalars[(i + 1)...]))
            }
        }
        if url.pyStarts("//") {
            let rest = Array(url.unicodeScalars.dropFirst(2))
            let end = rest.firstIndex(where: { $0 == "/" || $0 == "?" || $0 == "#" }) ?? rest.count
            netloc = String(String.UnicodeScalarView(rest[..<end]))
            let open = netloc.pyContains("["), close = netloc.pyContains("]")
            if open != close { return nil }
        }
    }

    /// _hostinfo
    var hostinfo: (String, String?) {
        let afterAt = netloc.pyRPartition("@").2
        let (_, br, bracketed) = afterAt.pyPartition("[")
        var hostname: String
        var port: String
        if br {
            let (h, _, p) = bracketed.pyPartition("]")
            hostname = h
            port = p.pyPartition(":").2
        } else {
            let (h, _, p) = afterAt.pyPartition(":")
            hostname = h
            port = p
        }
        return (hostname, port.isEmpty ? nil : port)
    }

    var hostname: String? {
        let h = hostinfo.0
        if h.isEmpty { return nil }
        let (name, pct, zone) = h.pyPartition("%")
        return name.lowercased() + (pct ? "%" : "") + zone
    }

    /// nil: invalid (ValueError); .some(nil): no port
    var port: Int?? {
        guard let p = hostinfo.1 else { return .some(nil) }
        guard p.unicodeScalars.allSatisfy({ $0.isASCII && ("0"..."9").contains($0) }), let n = Int(p), (0...65535).contains(n) else {
            return nil
        }
        return .some(n)
    }
}

/// The host's socket (orca-runtime.json) and CLI, for the live guard.
public final class OrcaClient: @unchecked Sendable {
    public let marker: String?
    let runtimePath: String
    let cli: String?
    let env: [String: String]

    public init(marker: String?, runtimePath: String, cli: String?, env: [String: String]) {
        self.marker = marker
        self.runtimePath = runtimePath
        self.cli = cli
        self.env = env
    }

    public static let methods: [OrcaRead: (String, PyJSON?)] = [
        .tabs: ("browser.tabList", .object(PyObject())),
        .worktrees: ("worktree.ps", .object(PyObject())),
        .terminals: ("terminal.list", .object(PyObject([("includeVisualLayouts", false)]))),
        .memory: ("diagnostics.memory", nil),
    ]

    /// The read's `result` when the answer says ok, else nil (Orca.call).
    public func call(_ read: OrcaRead, timeout: Double = 10) -> PyJSON? {
        guard cli != nil else { return nil }
        let (method, params) = Self.methods[read]!
        let data = rpc(method, params, timeout: timeout) ?? cliCall(read, timeout: timeout)
        guard let data, case .object(let o) = data, o["ok"]?.truthy ?? false else { return nil }
        return o["result"] ?? .null
    }

    public func rpc(_ method: String, _ params: PyJSON?, timeout: Double) -> PyJSON? {
        guard let meta = Files.json(runtimePath, fallback: .null).object else { return nil }
        var transports = meta["transports"]?.array ?? []
        if meta["transports"]?.array == nil { transports = [meta["transport"] ?? .object(PyObject())] }
        guard let endpoint = transports.compactMap({ t -> String? in
            guard let kind = t["kind"]?.string, kind == "unix" || kind == "named-pipe" else { return nil }
            return t["endpoint"]?.string
        }).first, let token = meta["authToken"] else { return nil }
        let id = UUIDText.random()
        var request = PyObject([("id", .string(id)), ("authToken", token), ("method", .string(method))])
        if let params { request["params"] = params }
        let found: PyJSON? = UnixLineClient.exchange(path: endpoint, line: PyJSON.object(request).dumps(), timeout: timeout) { line -> PyJSON? in
            guard let frame = try? PyJSON.loads(line) else { return .null }  // not JSON: Python gives up (ValueError)
            if frame["_keepalive"]?.truthy ?? false { return nil }
            return frame
        }
        guard let frame = found, frame != .null else { return nil }
        let runtime = frame["_meta"]?["runtimeId"]
        if frame["id"] != .string(id) { return nil }
        if let runtime, runtime.truthy, runtime != (meta["runtimeId"] ?? .null) { return nil }
        return frame
    }

    func cliCall(_ read: OrcaRead, timeout: Double) -> PyJSON? {
        guard let cli else { return nil }
        let words = read.rawValue.split(separator: " ").map(String.init) + ["--json"]
        guard let out = Spawn.output([cli] + words, env: env, timeout: timeout) else { return nil }
        return try? PyJSON.loads(bytes: out)
    }
}
