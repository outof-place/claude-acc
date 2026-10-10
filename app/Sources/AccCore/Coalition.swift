// What a launchd job costs, from its resource coalition: launchd gives every job its own, and the
// kernel keeps CPU time, wakeups, spawns and writes of every process that ever ran in it. Two reads
// a window apart measure a periodic job's runs and all their children, which no per-process sample
// can catch. No root needed.
import CAccCore
import Darwin

public struct CoalitionUsage: Equatable, Sendable {
    public var spawns: UInt64
    /// nanoseconds of CPU, all processes, alive or gone
    public var cpu: Double
    public var wakeups: UInt64
    public var written: UInt64
    /// nanojoules, as the kernel's energy model counts them
    public var energy: UInt64

    public static func read(_ cid: UInt64) -> CoalitionUsage? {
        var raw = acc_coalition_usage()
        guard acc_coalition_usage(cid, &raw) == 0 else { return nil }
        return CoalitionUsage(
            spawns: raw.tasks_started, cpu: Double(raw.cpu_time) * Kernel.nsPerTick,
            wakeups: raw.interrupt_wakeups &+ raw.platform_idle_wakeups, written: raw.byteswritten, energy: raw.energy)
    }

    /// Per second over `seconds`: CPU ms/s, wakeups/s, spawns, KB written/s, mW.
    public func rate(since old: CoalitionUsage, seconds: Double) -> Rate {
        let dt = max(seconds, 0.001)
        return Rate(
            cpuMsPerSecond: (cpu - old.cpu) / 1e6 / dt,
            wakeupsPerSecond: Double(wakeups &- old.wakeups) / dt,
            spawns: Int(spawns &- old.spawns),
            writtenKBPerSecond: Double(written &- old.written) / 1024 / dt,
            milliwatts: Double(energy &- old.energy) / 1e6 / dt)
    }

    public struct Rate: Equatable, Sendable {
        public var cpuMsPerSecond: Double
        public var wakeupsPerSecond: Double
        public var spawns: Int
        public var writtenKBPerSecond: Double
        public var milliwatts: Double
    }
}
