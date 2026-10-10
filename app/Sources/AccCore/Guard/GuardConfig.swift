// devguard.json over devguard_core.DEFAULT_CONFIG, read the way load_config reads it: the file's keys
// replace the defaults one level deep, whatever their type, and an unreadable file means defaults.
import Darwin

public struct GuardConfig: Sendable {
    public var raw: PyObject

    public static let defaults = PyObject([
        ("mode", "enforce"),
        ("interval_seconds", 5),
        ("orca_seconds", 10),
        ("budget_percent", 35),
        ("max_server_gb", 5),
        ("swap_warn_percent", 12),
        ("swap_critical_percent", 20),
        ("available_critical_percent", 10),
        ("available_warn_percent", 20),
        ("kernel_pressure", true),
        ("swapping_mb", 256),
        ("swapouts_burst", 1000),
        ("grace_minutes", 3),
        ("quiet_seconds", 30),
        ("duplicate_minutes", 5),
        ("orphan_minutes", 10),
        ("idle_minutes", 45),
        ("cooldown_seconds", 45),
        ("max_recycles_per_hour", 2),
        ("background_unattended", true),
        ("close_tabs", true),
        ("orca_comment", true),
        ("notify", true),
        ("protect", []),
        ("scope", []),
        ("runtimes", ["node", "bun", "deno"]),
        ("caps_minutes", 10),
        ("last_resort", true),
        ("restart_hold_minutes", 10),
        ("max_booted_simulators", 2),
        ("simulator_idle_minutes", 30),
        ("simulator_quiet_minutes", 5),
        ("simulator_pool_prefix", "Portivo-"),
        ("simulator_leases", "~/.cache/portivo-mobile/leases"),
        ("simulator_busy_cores", 0.15),
        ("simulator_protect", ["Portivo-Perf-*"]),
    ])

    public init(raw: PyObject) { self.raw = raw }

    /// DEFAULT_CONFIG updated with the file (a file that isn't a JSON object leaves the defaults).
    public static func load(path: String) -> GuardConfig {
        var cfg = defaults
        if let text = Files.read(path), let file = try? PyJSON.loads(bytes: text), case .object(let o) = file {
            for (k, v) in o { cfg[k] = v }
        }
        return GuardConfig(raw: cfg)
    }

    public subscript(key: String) -> PyJSON { raw[key] ?? .null }
    public func number(_ key: String) -> Double { self[key].double ?? 0 }
    public func flag(_ key: String) -> Bool { self[key].truthy }
    public func strings(_ key: String) -> [String] { self[key].array?.compactMap(\.string) ?? [] }
}

/// Small file helpers with Python's failure behavior: nil instead of an exception.
public enum Files {
    public static func read(_ path: String) -> [UInt8]? {
        let fd = open(path, O_RDONLY | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0 else { return nil }
        var out = [UInt8](repeating: 0, count: max(Int(st.st_size), 0) + 1)
        var used = 0
        while true {
            if used == out.count { out += [UInt8](repeating: 0, count: max(out.count, 4096)) }
            let n = out.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress! + used, $0.count - used) }
            if n < 0 { if errno == EINTR { continue }; return nil }
            if n == 0 { break }
            used += n
        }
        out.removeSubrange(used...)
        return out
    }

    /// janitor.load_json: the parsed file, or `fallback` when it's missing or not JSON.
    public static func json(_ path: String, fallback: PyJSON) -> PyJSON {
        guard let bytes = read(path), let value = try? PyJSON.loads(bytes: bytes) else { return fallback }
        return value
    }

    /// janitor.write_json: the whole text into a temp file next to it, then rename.
    @discardableResult
    public static func writeAtomic(_ path: String, _ text: String) -> Bool {
        let dir = dirname(path)
        mkdirs(dir)
        let tmp = "\(path).\(getpid()).tmp"
        let fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return false }
        var ok = true
        var bytes = Array(text.utf8)
        var off = 0
        while off < bytes.count {
            let n = bytes.withUnsafeMutableBytes { Darwin.write(fd, $0.baseAddress! + off, $0.count - off) }
            if n < 0 { if errno == EINTR { continue }; ok = false; break }
            off += n
        }
        bytes.removeAll()
        close(fd)
        guard ok, rename(tmp, path) == 0 else {
            unlink(tmp)
            return false
        }
        return true
    }

    public static func mkdirs(_ dir: String) {
        guard !dir.isEmpty, access(dir, F_OK) != 0 else { return }
        mkdirs(dirname(dir))
        mkdir(dir, 0o755)
    }

    public static func exists(_ path: String) -> Bool { access(path, F_OK) == 0 }

    public static func isDirectory(_ path: String) -> Bool {
        var st = stat()
        return stat(path, &st) == 0 && st.st_mode & S_IFMT == S_IFDIR
    }

    /// os.path.realpath(os.path.expanduser(path)): janitor.expand.
    public static func expand(_ path: String, home: String) -> String {
        var p = path
        if p == "~" { p = home } else if p.hasPrefix("~/") { p = home + p.dropFirst(1) }
        return realpath(p)
    }

    /// os.path.realpath: resolves what exists, keeps the rest as written (normalized).
    public static func realpath(_ path: String) -> String {
        if let resolved = Darwin.realpath(path, nil) {
            defer { free(resolved) }
            return String(cString: resolved)
        }
        // Python resolves the longest existing prefix and appends the rest, normalized
        let parts = normalize(path).split(separator: "/", omittingEmptySubsequences: true).map(String.init)
        var prefix = "/"
        var i = 0
        while i < parts.count {
            let next = prefix == "/" ? "/" + parts[i] : prefix + "/" + parts[i]
            guard let r = Darwin.realpath(next, nil) else { break }
            prefix = String(cString: r)
            free(r)
            i += 1
        }
        let rest = parts[i...].joined(separator: "/")
        return rest.isEmpty ? prefix : (prefix == "/" ? "/" + rest : prefix + "/" + rest)
    }

    /// os.path.normpath for absolute paths.
    public static func normalize(_ path: String) -> String {
        var out: [Substring] = []
        for part in path.split(separator: "/", omittingEmptySubsequences: true) {
            if part == "." { continue }
            if part == ".." { if !out.isEmpty { out.removeLast() }; continue }
            out.append(part)
        }
        return "/" + out.joined(separator: "/")
    }
}

