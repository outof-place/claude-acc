// The process table between ticks. Python reads every process's arguments on every tick (600 sysctls
// here, 5 ms natively, 12 ms in Python); this keeps each line until the process changes: a new pid or
// start time, an exec (kqueue NOTE_EXEC on each of our processes), a new name, uid or zombie state.
// Two cases have no event: a process that rewrites its own argv (node's process.title), so lines of
// processes younger than `youngSeconds` are read again every tick, and anything else is caught by a
// full read every `fullSeconds`. tests/acc_cored/parity_probes.py --cache compares it with a fresh read.
import Darwin

public final class ProcCache {
    struct Entry {
        var start: timeval
        var stat: CChar
        var uid: uid_t
        var comm: (UInt64, UInt64, UInt8)
        var command: String
        var facts: CommandFacts
        var tdev: Int32
        var ppid: pid_t
        var watched: Bool
    }

    public var youngSeconds = 60.0
    public var fullSeconds = 60.0
    var entries: [pid_t: Entry] = [:]
    let kq = kqueue()
    var lastFull = 0.0
    let me = getuid()
    public private(set) var argvReads = 0
    /// the host apps' main processes in the last read ("Pod:99902"): a change means the host may have
    public private(set) var hostApps = ""

    public init() {}
    deinit { close(kq) }

    /// Proc.all()'s rows, reading arguments only where something may have changed.
    public func rows(now: Double) -> [ProcRow] {
        var execed: Set<pid_t> = []
        var events: [kevent] = Array(repeating: kevent(ident: 0, filter: 0, flags: 0, fflags: 0, data: 0, udata: nil), count: 256)
        var zero = timespec()
        while true {
            let n = kevent(kq, nil, 0, &events, Int32(events.count), &zero)
            guard n > 0 else { break }
            for e in events.prefix(Int(n)) where e.fflags & UInt32(NOTE_EXEC) != 0 { execed.insert(pid_t(e.ident)) }
            if n < events.count { break }
        }
        let full = now - lastFull >= fullSeconds
        if full { lastFull = now }
        var seen: [pid_t: Entry] = [:]
        seen.reserveCapacity(entries.count + 64)
        var rows: [ProcRow] = []
        Proc.forEachKinfo { p in
            let pid = p.kp_proc.p_pid
            guard pid != 0 else { return }
            let start = p.kp_proc.p_un.__p_starttime
            let owner = p.kp_eproc.e_ucred.cr_uid
            let comm = withUnsafeBytes(of: p.kp_proc.p_comm) { raw in (raw.load(as: UInt64.self), raw.load(fromByteOffset: 8, as: UInt64.self), raw[16]) }
            let stat = p.kp_proc.p_stat
            let age = now - (Double(start.tv_sec) + Double(start.tv_usec) / 1e6)
            var entry: Entry
            if let old = entries[pid], old.start.tv_sec == start.tv_sec, old.start.tv_usec == start.tv_usec, old.stat == stat,
               old.uid == owner, old.comm == comm, !full, !execed.contains(pid), age >= youngSeconds
            {
                entry = old
                entry.ppid = p.kp_eproc.e_ppid
                entry.tdev = p.kp_eproc.e_tdev
            } else {
                let args = me == 0 || owner == me ? Args.read(pid) : nil
                if args != nil { argvReads += 1 }
                let command: String
                if let args, !args.argv.isEmpty {
                    command = Text.psCommand(args.argv)
                } else if stat == Proc.zombie {
                    command = "<defunct>"
                } else {
                    command = "(" + Text.comm(p.kp_proc.p_comm) + ")"
                }
                let watched = entries[pid].map { $0.watched && $0.start.tv_sec == start.tv_sec && $0.start.tv_usec == start.tv_usec } ?? false
                // a line read again but unchanged keeps what the patterns said about it
                let facts = entries[pid].flatMap { $0.command == command ? $0.facts : nil } ?? CommandFacts(command)
                entry = Entry(
                    start: start, stat: stat, uid: owner, comm: comm, command: command, facts: facts, tdev: p.kp_eproc.e_tdev,
                    ppid: p.kp_eproc.e_ppid, watched: watched)
                if !entry.watched && owner == me {
                    var change = kevent(ident: UInt(pid), filter: Int16(EVFILT_PROC), flags: UInt16(EV_ADD | EV_CLEAR), fflags: UInt32(NOTE_EXEC), data: 0, udata: nil)
                    entry.watched = kevent(kq, &change, 1, nil, 0, nil) == 0
                }
            }
            seen[pid] = entry
            rows.append(ProcRow(pid: pid, ppid: entry.ppid, command: entry.command, tdev: entry.tdev, uid: owner, facts: entry.facts))
        }
        entries = seen
        hostApps = rows.filter { r in
            ["Pod", "Orca"].contains(r.facts.program) && firstWord(r.command).pyEnds("/Contents/MacOS/" + r.facts.program)
        }.map { "\($0.facts.program):\($0.pid)" }.sorted().joined(separator: ",")
        rows.sort { ($0.tdev, $0.pid) < ($1.tdev, $1.pid) }
        return rows
    }
}
