// Phase timings for `acc-cored guard-bench`: CPU time of the calling thread per named phase.
import Darwin

public enum Profile {
    nonisolated(unsafe) public static var on = false
    nonisolated(unsafe) public static var totals: [String: Double] = [:]
    nonisolated(unsafe) public static var walls: [String: Double] = [:]
    nonisolated(unsafe) static var order: [String] = []

    @inline(__always) static func cpu() -> Double { Double(clock_gettime_nsec_np(CLOCK_THREAD_CPUTIME_ID)) }

    @inline(__always)
    public static func measure<T>(_ name: String, _ body: () throws -> T) rethrows -> T {
        guard on else { return try body() }
        let t0 = cpu()
        let w0 = Double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW))
        defer {
            if totals[name] == nil { order.append(name) }
            totals[name, default: 0] += cpu() - t0
            walls[name, default: 0] += Double(clock_gettime_nsec_np(CLOCK_UPTIME_RAW)) - w0
        }
        return try body()
    }

    nonisolated(unsafe) public static var resetAt = 0.0

    public static func reset() {
        totals.removeAll()
        walls.removeAll()
        order.removeAll()
        resetAt = Double(clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID))
    }

    public static func report(ticks: Int) -> String {
        order.map { name in
            let ms = totals[name]! / 1e6 / Double(max(ticks, 1))
            let wall = walls[name]! / 1e6 / Double(max(ticks, 1))
            return "\(name) \(GuardText.fixed(ms, 3)) ms CPU, \(GuardText.fixed(wall, 3)) ms wall"
        }.joined(separator: "\n")
    }
}
