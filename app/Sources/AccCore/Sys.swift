// Small system helpers without Foundation: a uuid4 string, one request/answer over a Unix socket,
// and a subprocess with a deadline for the cold paths that still shell out (the host's CLI, Python).
import Darwin

/// A NUL-terminated C buffer as text
public func cText(_ buffer: [CChar]) -> String {
    buffer.withUnsafeBufferPointer { raw in
        let bytes = UnsafeRawBufferPointer(raw).prefix { $0 != 0 }
        return String(decoding: bytes, as: UTF8.self)
    }
}

public enum UUIDText {
    /// str(uuid.uuid4())
    public static func random() -> String {
        var bytes = [UInt8](repeating: 0, count: 16)
        arc4random_buf(&bytes, 16)
        bytes[6] = bytes[6] & 0x0F | 0x40
        bytes[8] = bytes[8] & 0x3F | 0x80
        let hex = bytes.map { b in (b < 16 ? "0" : "") + String(b, radix: 16) }
        return hex[0..<4].joined() + "-" + hex[4..<6].joined() + "-" + hex[6..<8].joined() + "-" + hex[8..<10].joined() + "-"
            + hex[10..<16].joined()
    }
}

public enum UnixLineClient {
    /// Connects, sends `line` + "\n", and reads newline-delimited frames until `stop` returns a value
    /// (nil from `stop` means "not this one, read on"); nil on any failure or after `timeout` seconds.
    public static func exchange<T>(path: String, line: String, timeout: Double, stop: (String) -> T?) -> T? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { return nil }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        var tv = timeval(tv_sec: Int(timeout), tv_usec: Int32((timeout - Double(Int(timeout))) * 1e6))
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard ok == 0 else { return nil }
        var out = Array((line + "\n").utf8)
        var sent = 0
        while sent < out.count {
            let n = out.withUnsafeBytes { send(fd, $0.baseAddress! + sent, $0.count - sent, 0) }
            if n <= 0 { if n < 0 && errno == EINTR { continue }; return nil }
            sent += n
        }
        out.removeAll()
        let deadline = Kernel.wall() + timeout
        var buffer: [UInt8] = []
        var chunk = [UInt8](repeating: 0, count: 1 << 16)
        while true {
            while let nl = buffer.firstIndex(of: 0x0A) {
                let frame = String(decoding: buffer[..<nl], as: UTF8.self)
                buffer.removeSubrange(...nl)
                if frame.unicodeScalars.allSatisfy({ $0 == " " || $0 == "\t" || $0 == "\r" }) { continue }
                if let value = stop(frame) { return value }
            }
            guard Kernel.wall() < deadline else { return nil }
            let n = chunk.withUnsafeMutableBytes { recv(fd, $0.baseAddress, $0.count, 0) }
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { return nil }
            buffer += chunk[..<n]
        }
    }
}

public enum Spawn {
    /// Runs argv (PATH lookup like execvp), stdin from /dev/null, stderr dropped; its stdout, or nil
    /// when it can't start or outlives `timeout` (then its process group is killed). The child leads
    /// a group of its own, so that kill reaches what it started too.
    public static func output(_ argv: [String], env: [String: String]? = nil, timeout: Double) -> [UInt8]? {
        var pipe: [Int32] = [0, 0]
        guard Darwin.pipe(&pipe) == 0 else { return nil }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, pipe[1], 1)
        posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        posix_spawn_file_actions_addclose(&actions, pipe[0])
        posix_spawn_file_actions_addclose(&actions, pipe[1])
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETPGROUP))
        posix_spawnattr_setpgroup(&attr, 0)
        var all = sigset_t()
        sigfillset(&all)
        posix_spawnattr_setsigdefault(&attr, &all)
        let cargs = argv.map { strdup($0) } + [nil]
        defer { cargs.forEach { free($0) } }
        let environment = (env ?? currentEnv()).map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { environment.forEach { free($0) } }
        var pid: pid_t = 0
        let rc = posix_spawnp(&pid, argv[0], &actions, &attr, cargs, environment)
        close(pipe[1])
        guard rc == 0 else {
            close(pipe[0])
            return nil
        }
        var out: [UInt8] = []
        var chunk = [UInt8](repeating: 0, count: 1 << 16)
        let deadline = Kernel.wall() + timeout
        var fds = pollfd(fd: pipe[0], events: Int16(POLLIN), revents: 0)
        var timedOut = false
        while true {
            let left = deadline - Kernel.wall()
            if left <= 0 { timedOut = true; break }
            let r = poll(&fds, 1, Int32(min(left, 3600) * 1000))
            if r < 0 { if errno == EINTR { continue }; break }
            if r == 0 { timedOut = true; break }
            let n = chunk.withUnsafeMutableBytes { read(pipe[0], $0.baseAddress, $0.count) }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            out += chunk[..<n]
        }
        close(pipe[0])
        // a child that closed its stdout may still run: it gets what is left of the deadline
        let status = reap(pid, deadline: timedOut ? Kernel.wall() : deadline)
        return timedOut || status == nil ? nil : out
    }

    /// Waits for `pid` until `deadline` (kqueue NOTE_EXIT), then kills its group and waits for it;
    /// its wait status, or nil when it had to be killed.
    static func reap(_ pid: pid_t, deadline: Double) -> Int32? {
        var status: Int32 = 0
        let kq = kqueue()
        defer { close(kq) }
        var change = kevent(ident: UInt(pid), filter: Int16(EVFILT_PROC), flags: UInt16(EV_ADD | EV_ONESHOT), fflags: NOTE_EXIT, data: 0, udata: nil)
        var event = kevent()
        // ESRCH: it has already ended (or is ending); NOTE_EXIT comes while it exits, a moment
        // before waitpid can reap it, so both wait for it rather than ask once
        var exited = kevent(kq, &change, 1, nil, 0, nil) != 0
        while !exited {
            let left = deadline - Kernel.wall()
            if left <= 0 { break }
            let n: Int32
            if left > 1e9 {
                n = kevent(kq, nil, 0, &event, 1, nil)  // no deadline
            } else {
                var wait = timespec(tv_sec: Int(left), tv_nsec: Int((left - left.rounded(.down)) * 1e9))
                n = kevent(kq, nil, 0, &event, 1, &wait)
            }
            if n < 0 && errno == EINTR { continue }
            exited = n > 0
            break
        }
        var r: pid_t
        repeat { r = waitpid(pid, &status, exited ? 0 : WNOHANG) } while r < 0 && errno == EINTR
        if r == pid { return status }
        killpg(pid, SIGKILL)
        kill(pid, SIGKILL)
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        return nil
    }

    /// Runs argv to its end with stdout/stderr appended to `log`; the exit code, or 124 when it
    /// outlives `timeout` (its group is killed, as `timeout(1)` answers).
    public static func run(_ argv: [String], env: [String: String]? = nil, log: String? = nil, timeout: Double = .infinity) -> Int32 {
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        let target = log ?? "/dev/null"
        posix_spawn_file_actions_addopen(&actions, 1, target, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        posix_spawn_file_actions_adddup2(&actions, 1, 2)
        var attr: posix_spawnattr_t?
        posix_spawnattr_init(&attr)
        defer { posix_spawnattr_destroy(&attr) }
        posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETPGROUP))
        posix_spawnattr_setpgroup(&attr, 0)
        var all = sigset_t()
        sigfillset(&all)
        posix_spawnattr_setsigdefault(&attr, &all)
        let cargs = argv.map { strdup($0) } + [nil]
        defer { cargs.forEach { free($0) } }
        let environment = (env ?? currentEnv()).map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { environment.forEach { free($0) } }
        var pid: pid_t = 0
        guard posix_spawnp(&pid, argv[0], &actions, &attr, cargs, environment) == 0 else { return 127 }
        guard let status = reap(pid, deadline: Kernel.wall() + timeout) else { return 124 }
        if status & 0x7f == 0 { return (status >> 8) & 0xff }
        return 128 + (status & 0x7f)
    }

    public static func currentEnv() -> [String: String] {
        var out: [String: String] = [:]
        var p = environ
        while let entry = p.pointee {
            let s = String(cString: entry)
            if let eq = s.firstIndex(of: "=") { out[String(s[..<eq])] = String(s[s.index(after: eq)...]) }
            p += 1
        }
        return out
    }
}
