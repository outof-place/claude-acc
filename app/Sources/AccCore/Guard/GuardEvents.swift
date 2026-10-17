// The decision registry ($STATE/events.jsonl, events.py) as the guard writes it: the `stage` row of a
// tick that changed the brake's stage. Rows of everything Python acts on (`stop`, `kill`, `runaway`)
// come from Python's own tick after a handover, so the native side writes only what it decides
// alone. Same file, same lock, same row: the clocks first ({ts, up, boot, ev}), then lastresort's
// signals of that moment, then the decision's fields.
import Darwin

public enum GuardEvents {
    public static let name = "events.jsonl"
    static let lockName = "events.lock"
    /// events.py ROTATE_BYTES: past this the file goes to events.jsonl.1 (the old .1 goes)
    public static let rotateBytes = 32 * 1024 * 1024
    /// events.py LOCK_WAIT_S: a writer stopped with the lock (SIGSTOP) can't stop the guard
    static let lockWait = 1.0
    /// lastresort.SIGNALS, in its order
    static let signalNames = [
        "compressed", "segments", "segments_limit", "swap_used", "swap_growth", "swapouts", "kernel", "available",
        "swap_window_s",
    ]

    /// events.boot(): kern.bootsessionuuid, nil when the sysctl fails
    public static let boot: String? = {
        var buf = [CChar](repeating: 0, count: 64)
        var size = buf.count
        guard sysctlbyname("kern.bootsessionuuid", &buf, &size, nil, 0) == 0 else { return nil }
        let value = String(cString: buf)
        return value.isEmpty ? nil : value
    }()

    /// events.uptime(): seconds since boot without sleep (CLOCK_UPTIME_RAW), as time.clock_gettime
    public static func uptime() -> Double {
        var spec = timespec()
        clock_gettime(CLOCK_UPTIME_RAW, &spec)
        return Double(spec.tv_sec) + Double(spec.tv_nsec) * 1e-9
    }

    /// lastresort.disk_free_gb(): the data volume's free space in GB, one decimal, nil when unreadable
    public static func diskFreeGB(_ path: String = "/System/Volumes/Data") -> PyJSON {
        var st = statvfs()
        guard statvfs(path, &st) == 0 else { return .null }
        return .double(GuardText.round(Double(st.f_bavail) * Double(st.f_frsize) / Double(GuardText.GB), 1))
    }

    /// lastresort.signals(pressure): the reasons and memory signals of one moment
    public static func signals(_ p: Pressure, diskFree: PyJSON) -> PyObject {
        var o = PyObject()
        o["stage_reasons"] = .array(p.stageReasons.map { .string($0) })
        o["guard_level"] = .int(p.level)
        o["guard_reasons"] = .array(p.reasons.map { .string($0) })
        let values: [String: PyJSON] = [
            "compressed": .int(p.compressed), "segments": p.segments.map { .int($0) } ?? .null,
            "segments_limit": p.segmentsLimit.map { .int($0) } ?? .null, "swap_used": .int(p.swapUsed),
            "swap_growth": .int(p.swapGrowth), "swapouts": .int(p.swapouts), "kernel": .int(p.kernel),
            "available": p.available.map { .int($0) } ?? .null, "swap_window_s": .double(p.swapWindowS),
        ]
        for key in signalNames { o[key] = values[key] ?? .null }
        o["disk_free_gb"] = diskFree
        return o
    }

    /// devguard_core.record(ev, world, **fields): signals, then the decision's fields; false when the
    /// row wasn't written (the lock held past a second, a write that failed). Never throws.
    @discardableResult
    public static func record(
        _ ev: String, pressure: Pressure, fields: PyObject, stateDir: String, now: Double = Kernel.wall(),
        up: Double = uptime(), diskFree: PyJSON = diskFreeGB()
    ) -> Bool {
        var row = signals(pressure, diskFree: diskFree)
        for (k, v) in fields { row[k] = v }
        return append(stateDir: stateDir, ev: ev, fields: row, now: now, up: up)
    }

    /// events.append(state_dir, ev, **fields): {ts, up, boot, ev, **fields} as one line, the caller's
    /// fields never over the clocks or the kind; under flock on events.lock, one O_APPEND write.
    @discardableResult
    public static func append(
        stateDir: String, ev: String, fields: PyObject, now: Double, up: Double, rotateAt: Int = rotateBytes
    ) -> Bool {
        Files.mkdirs(stateDir)
        let lock = open(stateDir + "/" + lockName, O_RDWR | O_CREAT | O_CLOEXEC, 0o644)
        guard lock >= 0 else { return false }
        defer { close(lock) }  // closing lets the flock go
        guard take(lock) else { return false }
        var row = PyObject([("ts", .double(now)), ("up", .double(up)), ("boot", boot.map { .string($0) } ?? .null), ("ev", .string(ev))])
        for (k, v) in fields where row[k] == nil { row[k] = v }
        let path = stateDir + "/" + name
        rotate(path, at: rotateAt)
        let line = Array((PyJSON.object(row).dumps(ensureASCII: false) + "\n").utf8)
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        return line.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) } == line.count
    }

    /// events.rotate: at ROTATE_BYTES the file goes to <file>.1, under the registry's lock
    static func rotate(_ path: String, at limit: Int) {
        var st = stat()
        if stat(path, &st) == 0, Int(st.st_size) >= limit { rename(path, path + ".1") }
    }

    /// events._lock: flock without blocking for longer than LOCK_WAIT_S
    static func take(_ fd: Int32) -> Bool {
        let deadline = uptime() + lockWait
        while flock(fd, LOCK_EX | LOCK_NB) != 0 {
            guard errno == EWOULDBLOCK, uptime() < deadline else { return false }
            usleep(2000)
        }
        return true
    }
}
