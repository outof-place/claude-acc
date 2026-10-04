import Darwin
import Foundation
import IOKit

/// CPU and GPU load as Activity Monitor would show it, read straight from the kernel: no
/// helper, no root, nothing spawned. Sampled only while the panel is open.
struct LoadReading: Equatable {
    /// Busy share of all cores, 0-100.
    let cpu: Double
    /// The same split by core type: performance and efficiency cores.
    let pCores: Double?
    let eCores: Double?
    /// "Device Utilization %" of the GPU, 0-100.
    let gpu: Double?
}

final class LoadSampler {
    private var last: [[UInt32]]?
    /// Logical CPUs of the efficiency cluster. Apple silicon numbers them first: under
    /// background-only load (taskpolicy -b) exactly CPUs 0-3 of an M4 Max go busy.
    private let efficiency = LoadSampler.sysctl("hw.perflevel1.logicalcpu") ?? 0

    /// Load since the previous call; nil on the first, which only sets the baseline.
    func sample() -> LoadReading? {
        guard let now = Self.ticks() else { return nil }
        defer { last = now }
        guard let last, last.count == now.count else { return nil }
        var busy = [Double](), total = [Double]()
        for (a, b) in zip(last, now) {
            // user, system, idle, nice; the counters wrap, hence &-
            let used = Double(b[0] &- a[0]) + Double(b[1] &- a[1]) + Double(b[3] &- a[3])
            busy.append(used)
            total.append(used + Double(b[2] &- a[2]))
        }
        func share(_ range: Range<Int>) -> Double? {
            guard !range.isEmpty, range.upperBound <= busy.count else { return nil }
            let all = range.reduce(0) { $0 + total[$1] }
            return all > 0 ? range.reduce(0) { $0 + busy[$1] } / all * 100 : nil
        }
        let split = efficiency > 0 && efficiency < busy.count
        return LoadReading(
            cpu: share(0..<busy.count) ?? 0,
            pCores: split ? share(efficiency..<busy.count) : nil,
            eCores: split ? share(0..<efficiency) : nil,
            gpu: Self.gpu())
    }

    private static func ticks() -> [[UInt32]]? {
        var count: natural_t = 0
        var info: processor_info_array_t?
        var infoCount: mach_msg_type_number_t = 0
        guard host_processor_info(mach_host_self(), PROCESSOR_CPU_LOAD_INFO, &count, &info, &infoCount) == KERN_SUCCESS,
              let info else { return nil }
        defer {
            vm_deallocate(
                mach_task_self_, vm_address_t(bitPattern: info),
                vm_size_t(Int(infoCount) * MemoryLayout<integer_t>.stride))
        }
        let states = Int(CPU_STATE_MAX)
        return (0..<Int(count)).map { cpu in
            (0..<states).map { UInt32(bitPattern: info[cpu * states + $0]) }
        }
    }

    /// The busiest GPU's utilisation from its IOAccelerator statistics.
    private static func gpu() -> Double? {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOAccelerator"), &iterator)
            == KERN_SUCCESS else { return nil }
        defer { IOObjectRelease(iterator) }
        var best: Double?
        while case let entry = IOIteratorNext(iterator), entry != 0 {
            defer { IOObjectRelease(entry) }
            let stats = IORegistryEntryCreateCFProperty(entry, "PerformanceStatistics" as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? [String: Any]
            if let value = (stats?["Device Utilization %"] as? NSNumber)?.doubleValue {
                best = max(best ?? 0, value)
            }
        }
        return best
    }

    private static func sysctl(_ name: String) -> Int? {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname(name, &value, &size, nil, 0) == 0 ? Int(value) : nil
    }
}
