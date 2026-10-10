// The kernel readings acc-cored's loops are built on, without a subprocess: sysctl, mach time and
// proc_pid_rusage. Each mirrors the reader devguard_core.py has used through ctypes, value for value,
// so a reading here and there of the same moment agree (tests/acc_cored/parity_probes.py checks the
// tables built on them).
import Darwin

public enum Kernel {
    /// Nanoseconds per mach absolute time unit (125/3 on Apple silicon, 1 on Intel).
    public static let nsPerTick: Double = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return info.denom == 0 ? 1 : Double(info.numer) / Double(info.denom)
    }()

    /// A sysctl integer of any width, as devguard's `sysctl_int`: the value masked to the size the
    /// kernel wrote, nil when the name doesn't exist on this macOS.
    public static func int(_ name: String) -> UInt64? {
        var value: UInt64 = 0
        var size = MemoryLayout<UInt64>.size
        guard sysctlbyname(name, &value, &size, nil, 0) == 0 else { return nil }
        return size >= 8 ? value : value & ((1 << (8 * UInt64(size))) - 1)
    }

    /// vm.swapusage: (total, used) bytes, (0, 0) when unreadable.
    public static func swap() -> (total: UInt64, used: UInt64) {
        var usage = xsw_usage()
        var size = MemoryLayout<xsw_usage>.size
        guard sysctlbyname("vm.swapusage", &usage, &size, nil, 0) == 0 else { return (0, 0) }
        return (usage.xsu_total, usage.xsu_used)
    }

    /// Wall clock in seconds, as Python's time.time().
    public static func wall() -> Double {
        var spec = timespec()
        clock_gettime(CLOCK_REALTIME, &spec)
        return Double(spec.tv_sec) + Double(spec.tv_nsec) / 1e9
    }
}

/// One process's proc_pid_rusage (RUSAGE_INFO_V6), the fields the guard and the scheduler use.
public struct Usage: Equatable, Sendable {
    public var footprint: UInt64
    /// lifetime_max_phys_footprint: the biggest the process has ever been
    public var peak: UInt64
    /// user + system CPU in seconds
    public var cpu: Double
    public var written: UInt64
    /// proc_start_abstime, the process's identity together with its pid
    public var start: UInt64
    public var wakeups: UInt64

    public init(footprint: UInt64, peak: UInt64, cpu: Double, written: UInt64, start: UInt64, wakeups: UInt64 = 0) {
        self.footprint = footprint
        self.peak = peak
        self.cpu = cpu
        self.written = written
        self.start = start
        self.wakeups = wakeups
    }

    /// nil for a process that is gone (or another user's process the kernel won't describe).
    public static func of(_ pid: pid_t) -> Usage? {
        var info = rusage_info_v6()
        let ok = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V6, $0) }
        }
        guard ok == 0 else { return nil }
        return Usage(
            footprint: info.ri_phys_footprint, peak: info.ri_lifetime_max_phys_footprint,
            cpu: Double(info.ri_user_time &+ info.ri_system_time) * Kernel.nsPerTick / 1e9,
            written: info.ri_diskio_byteswritten, start: info.ri_proc_start_abstime,
            wakeups: info.ri_pkg_idle_wkups &+ info.ri_interrupt_wkups)
    }
}
