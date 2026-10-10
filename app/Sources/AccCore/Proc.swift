// The process table as devguard_core.processes() builds it: one KERN_PROC_ALL sysctl, and the
// arguments of our own processes from KERN_PROCARGS2, turned into the line `ps -axo command=` shows.
import Darwin

/// One row of the table: what `ps -axo pid=,ppid=,command=` prints, plus the terminal it sorts by.
public struct ProcRow: Equatable {
    public var pid: pid_t
    public var ppid: pid_t
    public var command: String
    public var tdev: Int32
    public var uid: uid_t
    /// what the guard's patterns say about `command`, shared while the line stays the same
    public var facts: CommandFacts

    public init(pid: pid_t, ppid: pid_t, command: String, tdev: Int32 = -1, uid: uid_t = 0, facts: CommandFacts? = nil) {
        self.pid = pid
        self.ppid = ppid
        self.command = command
        self.tdev = tdev
        self.uid = uid
        self.facts = facts ?? CommandFacts(command)
    }

    public static func == (a: ProcRow, b: ProcRow) -> Bool {
        a.pid == b.pid && a.ppid == b.ppid && a.command.pyEq(b.command) && a.tdev == b.tdev && a.uid == b.uid
    }
}

public enum Proc {
    static let zombie: CChar = 5  // SZOMB in <sys/proc.h>

    /// Every process, sorted as ps sorts them: by terminal (none first), then pid.
    public static func all() -> [ProcRow] {
        let me = getuid()
        var rows: [ProcRow] = []
        forEachKinfo { p in
            let pid = p.kp_proc.p_pid
            guard pid != 0 else { return }  // kernel_task: ps -ax doesn't show it
            let owner = p.kp_eproc.e_ucred.cr_uid
            let args = me == 0 || owner == me ? Args.read(pid) : nil
            var command: String
            if let args, !args.argv.isEmpty {
                command = Text.psCommand(args.argv)
            } else if p.kp_proc.p_stat == zombie {
                command = "<defunct>"
            } else {
                command = "(" + Text.comm(p.kp_proc.p_comm) + ")"
            }
            rows.append(ProcRow(pid: pid, ppid: p.kp_eproc.e_ppid, command: command, tdev: p.kp_eproc.e_tdev, uid: owner))
        }
        rows.sort { ($0.tdev, $0.pid) < ($1.tdev, $1.pid) }
        return rows
    }

    /// Calls `body` with each kinfo_proc of one KERN_PROC_ALL read, in the kernel's order.
    public static func forEachKinfo(_ body: (kinfo_proc) -> Void) {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_ALL]
        let stride = MemoryLayout<kinfo_proc>.stride
        for _ in 0..<5 {
            var size = 0
            guard sysctl(&mib, 3, nil, &size, nil, 0) == 0 else { return }
            // room for the processes born between the size and the read: a Go build adds hundreds,
            // and an empty table would be a tick without servers
            size += max(64 * stride, size / 4)
            let buffer = UnsafeMutableRawPointer.allocate(byteCount: size, alignment: 16)
            defer { buffer.deallocate() }
            guard sysctl(&mib, 3, buffer, &size, nil, 0) == 0 else { continue }
            let count = size / stride
            let procs = buffer.bindMemory(to: kinfo_proc.self, capacity: count)
            for i in 0..<count { body(procs[i]) }
            return
        }
    }

    /// The process's working directory (PROC_PIDVNODEPATHINFO), nil when unreadable.
    public static func cwd(_ pid: pid_t) -> String? {
        var info = proc_vnodepathinfo()
        let size = Int32(MemoryLayout<proc_vnodepathinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, &info, size) == size else { return nil }
        let path = withUnsafeBytes(of: &info.pvi_cdir.vip_path) { raw in
            Text.decode(raw.prefix(while: { $0 != 0 }))
        }
        return path.isEmpty ? nil : path
    }

    /// Whether `pid` is still the live process that started at `start`: pids get reused, and a zombie
    /// its parent hasn't reaped yet still answers rusage (but not bsdinfo).
    public static func alive(_ pid: pid_t, start: UInt64) -> Bool {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size, info.pbi_status != UInt32(zombie) else {
            return false
        }
        return Usage.of(pid)?.start == start
    }

    /// Direct children of `pid` (proc_listchildpids: the size goes in as bytes, the answer comes back
    /// as a count of pids).
    public static func children(of pid: pid_t) -> [pid_t] {
        var buffer = [pid_t](repeating: 0, count: 256)
        while true {
            let n = buffer.withUnsafeMutableBytes { proc_listchildpids(pid, $0.baseAddress, Int32($0.count)) }
            guard n > 0 else { return [] }
            let count = Int(n)
            if count < buffer.count { return Array(buffer.prefix(count).filter { $0 > 0 }) }
            buffer = [pid_t](repeating: 0, count: buffer.count * 2)
        }
    }
}

/// KERN_PROCARGS2: argc, the executable path, argv and the environment of one process.
public struct Args: Sendable {
    public var argc: Int
    /// the first `argc` NUL-separated strings after the path (fewer when the block was cut)
    public var argv: [[UInt8]]
    /// what follows argv: the environment, an empty string, then the kernel's apple[] strings
    var tail: Pieces

    /// One 64 KiB read covers almost every process; a full buffer can mean the kernel cut the
    /// arguments silently, so then the size is asked and read again (as devguard does).
    public static func read(_ pid: pid_t) -> Args? {
        guard let raw = raw(pid) else { return nil }
        return parse(raw)
    }

    public static func raw(_ pid: pid_t) -> [UInt8]? {
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        var size = buffer.count
        guard buffer.withUnsafeMutableBytes({ sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }) == 0 else { return nil }
        if size >= buffer.count {
            size = 0
            guard sysctl(&mib, 3, nil, &size, nil, 0) == 0, size > 0 else { return nil }
            buffer = [UInt8](repeating: 0, count: size)
            guard buffer.withUnsafeMutableBytes({ sysctl(&mib, 3, $0.baseAddress, &size, nil, 0) }) == 0 else { return nil }
        }
        guard size >= 4 else { return nil }
        buffer.removeSubrange(size...)
        return buffer
    }

    /// devguard's `procargs`: [argc] + rest.lstrip(b"\0").split(b"\0")[:argc] after the path.
    public static func parse(_ raw: [UInt8]) -> Args {
        let argc = Int(UInt32(raw[0]) | UInt32(raw[1]) << 8 | UInt32(raw[2]) << 16 | UInt32(raw[3]) << 24)
        var i = 4
        while i < raw.count, raw[i] != 0 { i += 1 }  // the executable path
        if i < raw.count { i += 1 }  // partition(b"\0") drops one NUL
        while i < raw.count, raw[i] == 0 { i += 1 }  // lstrip
        var pieces = Pieces(bytes: raw[i...])
        var argv: [[UInt8]] = []
        while argv.count < argc, let piece = pieces.next() { argv.append(Array(piece)) }
        return Args(argc: argc, argv: argv, tail: pieces)
    }

    /// devguard's `proc_env`: the strings after argv up to the first empty one, NAME=value only.
    public var environment: [String: String] {
        var env: [String: String] = [:]
        var pieces = tail
        while let item = pieces.next(), !item.isEmpty {
            guard let eq = item.firstIndex(of: UInt8(ascii: "=")) else { continue }
            env[Text.decode(item[item.startIndex..<eq])] = Text.decode(item[(eq + 1)...])
        }
        return env
    }

    /// argv decoded, only when complete (devguard's `proc_argv`: ps joins with spaces and loses quotes).
    public var exact: [String]? { argv.count == argc ? argv.map(Text.decode) : nil }
}

/// bytes.split(b"\0") one piece at a time: n NULs give n + 1 pieces, the last one possibly empty.
public struct Pieces: Sendable {
    var bytes: ArraySlice<UInt8>
    var done = false

    init(bytes: ArraySlice<UInt8>) { self.bytes = bytes }

    mutating func next() -> ArraySlice<UInt8>? {
        guard !done else { return nil }
        if let nul = bytes.firstIndex(of: 0) {
            defer { bytes = bytes[(nul + 1)...] }
            return bytes[bytes.startIndex..<nul]
        }
        done = true
        return bytes
    }
}

/// Text as Python's devguard makes it, so a command line compares equal byte for byte.
public enum Text {
    /// bytes.decode(errors="replace")
    public static func decode<C: Collection<UInt8>>(_ bytes: C) -> String {
        String(decoding: bytes, as: UTF8.self)
    }

    /// b" ".join(argv).decode(errors="replace").translate(PS_VIS): control characters as ps shows them.
    public static func psCommand(_ argv: [[UInt8]]) -> String {
        var joined: [UInt8] = []
        joined.reserveCapacity(argv.reduce(argv.count) { $0 + $1.count })
        for (i, arg) in argv.enumerated() {
            if i > 0 { joined.append(0x20) }
            joined.append(contentsOf: arg)
        }
        let text = decode(joined)
        guard text.utf8.contains(where: { $0 < 32 || $0 == 127 }) else { return text }
        var out = String.UnicodeScalarView()
        for scalar in text.unicodeScalars {
            switch scalar.value {
            case 9: out.append(contentsOf: "\\011".unicodeScalars)
            case 10: out.append(contentsOf: "\\012".unicodeScalars)
            case 127: out.append(contentsOf: "^?".unicodeScalars)
            case 0..<32:
                out.append("^")
                out.append(Unicode.Scalar(UInt8(scalar.value + 64)))
            default: out.append(scalar)
            }
        }
        return String(out)
    }

    /// p_comm: up to 16 characters, NUL-terminated.
    public static func comm<T>(_ tuple: T) -> String {
        withUnsafeBytes(of: tuple) { raw in decode(raw.prefix(17).prefix(while: { $0 != 0 })) }
    }
}
