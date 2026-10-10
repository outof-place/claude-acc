// Everything the guard reads from the system in one tick, behind one protocol: the live Mac
// (`LiveProbes`) or a recorded tick (`RecordedProbes`, tests/acc_cored/devguard_replay.py), so the
// same World and the same decisions run on both and the replay can compare them with Python's.
import Darwin

public protocol GuardProbes: AnyObject {
    var now: Double { get }
    /// mach_absolute_time() at the reading
    var absolute: UInt64 { get }
    var home: String { get }
    /// devguard_core.processes()
    func processes() -> [ProcRow]
    /// devguard_core.sockets()
    func sockets() -> SocketTable
    /// devguard_core.usage(pid)
    func usage(_ pid: pid_t) -> Usage?
    func cwd(_ pid: pid_t) -> String?
    /// devguard_core.proc_argv(pid): the exact argv, nil for another user's process or a cut block
    func argv(_ pid: pid_t) -> [String]?
    /// devguard_core.sysctl_int(name)
    func sysctl(_ name: String) -> UInt64?
    func swap() -> (total: UInt64, used: UInt64)
    /// proc_start_epoch(pid): pbi_start_tvsec, nil for a zombie or a gone process
    func startEpoch(_ pid: pid_t) -> Int?
    /// janitor.load_json(path, fallback) for the files the tick reads (pins, fsguard, leases, sched)
    func json(_ path: String, fallback: PyJSON) -> PyJSON
    /// The name in a simulator's device.plist, nil when unreadable
    func simulatorName(_ udid: String) -> String?
    /// The answer of one of the four Orca reads (devguard_core.ORCA_READS), nil on any failure
    func orca(_ read: OrcaRead) -> PyJSON?
    /// The host (Orca or Pod) whose main process marks it running: its main_marker
    var orcaMarker: String? { get }
}

public enum OrcaRead: String, Sendable, CaseIterable {
    case tabs = "tab list --worktree all"
    case worktrees = "worktree ps"
    case terminals = "terminal list"
    case memory = "diagnostics memory"
}

/// The live readers. One instance per tick: `now` and `absolute` are taken when it's made.
public final class LiveProbes: GuardProbes {
    public let now: Double
    public let absolute: UInt64
    public let home: String
    let orcaClient: OrcaClient?
    let cache: ProcCache?

    public init(home: String, orca: OrcaClient?, cache: ProcCache? = nil) {
        now = Kernel.wall()
        absolute = mach_absolute_time()
        self.home = home
        orcaClient = orca
        self.cache = cache
    }

    public func processes() -> [ProcRow] { cache?.rows(now: now) ?? Proc.all() }
    public func sockets() -> SocketTable { SocketTable.read() }
    public func usage(_ pid: pid_t) -> Usage? { Usage.of(pid) }
    public func cwd(_ pid: pid_t) -> String? { Proc.cwd(pid) }

    public func argv(_ pid: pid_t) -> [String]? {
        guard let args = Args.read(pid) else { return nil }
        return args.exact
    }

    public func sysctl(_ name: String) -> UInt64? { Kernel.int(name) }
    public func swap() -> (total: UInt64, used: UInt64) { Kernel.swap() }

    public func startEpoch(_ pid: pid_t) -> Int? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size, info.pbi_status != 5 else { return nil }
        return Int(info.pbi_start_tvsec)
    }

    public func json(_ path: String, fallback: PyJSON) -> PyJSON { Files.json(path, fallback: fallback) }

    public func simulatorName(_ udid: String) -> String? {
        SimulatorNames.shared.name(udid, home: home)
    }

    public func orca(_ read: OrcaRead) -> PyJSON? { orcaClient?.call(read) }
    public var orcaMarker: String? { orcaClient?.marker }
}
