// The janitor's sweep on acc-cored's clock: launchd starts `acc.py janitor sweep` at login and
// every 3 hours, whatever you're doing. Here the sweep waits, once it's due, until the Mac is
// idle: no keyboard or mouse for `idleSeconds` (IOKit's HIDIdleTime), no build in the scheduler,
// not on a low battery. After `maxDefer` past due it runs anyway (still not on a low battery).
// The sweep itself is Python's and keeps its own gates (min_hours_between_sweeps, the battery,
// five minutes after boot); it runs as launchd ran it, in the background band
// (`taskpolicy -b`: E cores, throttled IO) at nice 10.
// Nothing polls while the sweep isn't due: one timer at the due time, then one per idle check.
import Darwin
import Dispatch
import Foundation
import IOKit
import IOKit.ps

public final class JanitorEngine: @unchecked Sendable {
    public struct Options: Sendable {
        public var home: String
        public var python: [String]
        /// launchd's StartInterval for the janitor
        public var every = 3 * 3600.0
        public var idleSeconds = 5 * 60.0
        public var maxDefer = 6 * 3600.0

        public init(home: String, python: [String]? = nil) {
            self.home = home
            let state = GuardPaths.state(home)
            self.python = python ?? [state + "/python", state + "/acc.py"]
        }
    }

    /// What the decision reads; the live readers are below, tests pass their own.
    public struct Reading: Sendable {
        public var now: Double
        public var lastSweep: Double
        public var running: Bool
        public var idle: Double?
        public var builds: Int
        public var uptime: Double
        public var onBattery: Bool
        public var percent: Int
        public var minBattery: Int
    }

    public struct Decision: Equatable, Sendable {
        public var run: Bool
        /// when to look again (when not running now)
        public var next: Double
        public var why: String
    }

    let options: Options
    let queue = DispatchQueue(label: "acc-cored.janitor", qos: .utility)
    var timer: DispatchSourceTimer?
    var sweeping = false
    public private(set) var sweeps = 0
    /// tests: the sweep's run in place of Python's
    var runner: (@Sendable ([String]) -> Int32)?
    var reader: (() -> Reading)?

    public init(_ options: Options) {
        self.options = options
    }

    static func decide(_ r: Reading, every: Double, idleSeconds: Double, maxDefer: Double) -> Decision {
        let due = r.lastSweep + every
        if r.now < due { return Decision(run: false, next: due, why: "not due") }
        if r.running { return Decision(run: false, next: r.now + 600, why: "a sweep is running") }
        if r.uptime < 300 { return Decision(run: false, next: r.now + (300 - r.uptime), why: "just booted") }
        if r.onBattery && r.percent < r.minBattery { return Decision(run: false, next: r.now + 900, why: "low battery") }
        if r.now - due >= maxDefer { return Decision(run: true, next: 0, why: "overdue") }
        if r.builds > 0 { return Decision(run: false, next: r.now + 300, why: "builds running") }
        // no idle reading at all (no HID service): the deferral bound decides
        guard let idle = r.idle else { return Decision(run: false, next: due + maxDefer, why: "no idle time") }
        if idle < idleSeconds { return Decision(run: false, next: r.now + (idleSeconds - idle), why: "in use") }
        return Decision(run: true, next: 0, why: "idle")
    }

    // MARK: loop

    public func start() {
        queue.async { self.check() }
    }

    public func stop() {
        queue.sync { timer?.cancel() }
    }

    func check() {
        guard !sweeping else { return }
        let r = reader?() ?? Self.liveReading(home: options.home)
        let d = Self.decide(r, every: options.every, idleSeconds: options.idleSeconds, maxDefer: options.maxDefer)
        if d.run {
            sweeping = true
            let argv = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10"] + options.python + ["janitor", "sweep"]
            let run = runner
            let log = GuardPaths.state(options.home) + "/janitor-launchd.log"
            DispatchQueue.global(qos: .background).async {
                _ = run?(argv) ?? Spawn.run(argv, log: log, timeout: 3 * 3600)
                self.queue.async { self.finished() }
            }
            return
        }
        schedule(at: d.next)
    }

    func finished() {
        sweeping = false
        sweeps += 1
        check()
    }

    func schedule(at when: Double) {
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(wallDeadline: .now() + max(1, when - Kernel.wall()), leeway: .seconds(30))
        t.setEventHandler { [weak self] in self?.check() }
        t.resume()
        timer = t
    }

    /// `acc-cored janitor-check`: the live reading and what the engine would do now
    public static func describe(home: String) -> String {
        let r = liveReading(home: home)
        let o = Options(home: home)
        let d = decide(r, every: o.every, idleSeconds: o.idleSeconds, maxDefer: o.maxDefer)
        let idle = r.idle.map { GuardText.fixed($0, 1) + " s" } ?? "unknown"
        return "last sweep \(GuardText.round(r.now - r.lastSweep)) s ago, running \(r.running), idle \(idle), builds \(r.builds), "
            + "uptime \(GuardText.round(r.uptime)) s, battery \(r.onBattery ? "on" : "off") \(r.percent)%\n"
            + (d.run ? "would sweep now (\(d.why))" : "would look again in \(GuardText.round(d.next - r.now)) s (\(d.why))")
    }

    // MARK: live readings

    static func liveReading(home: String) -> Reading {
        let state = GuardPaths.state(home)
        let janitor = Files.json(state + "/janitor-state.json", fallback: .null).object ?? PyObject()
        let cfg = Files.json(state + "/janitor.json", fallback: .null).object ?? PyObject()
        let sched = Files.json(state + "/sched/state.json", fallback: .null).object ?? PyObject()
        let builds = (sched["running"]?.array ?? []).filter { ($0["where"]?.string ?? "local") == "local" }.count
        let (onBattery, percent) = battery()
        return Reading(
            now: Kernel.wall(), lastSweep: janitor["last_sweep"]?["at"]?.double ?? 0,
            running: locked(state + "/janitor.lock"), idle: hidIdle(), builds: builds,
            uptime: uptime(), onBattery: onBattery, percent: percent, minBattery: cfg["min_battery_percent"]?.int ?? 30)
    }

    /// Whether a sweep holds janitor.lock (janitor.take_lock), asked without waiting. running_since in
    /// janitor-state.json would outlive a sweep that crashed; the lock goes with its process.
    static func locked(_ path: String) -> Bool {
        let fd = open(path, O_RDONLY | O_CLOEXEC)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        if flock(fd, LOCK_EX | LOCK_NB) == 0 {
            flock(fd, LOCK_UN)
            return false
        }
        return errno == EWOULDBLOCK
    }

    /// Seconds since the last keyboard or mouse event (IOHIDSystem's HIDIdleTime, in ns).
    static func hidIdle() -> Double? {
        let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOHIDSystem"))
        guard service != 0 else { return nil }
        defer { IOObjectRelease(service) }
        guard let value = IORegistryEntryCreateCFProperty(service, "HIDIdleTime" as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue() as? Int64
        else { return nil }
        return Double(value) / 1e9
    }

    static func uptime() -> Double {
        var boot = timeval()
        var size = MemoryLayout<timeval>.size
        var mib: [Int32] = [CTL_KERN, KERN_BOOTTIME]
        guard sysctl(&mib, 2, &boot, &size, nil, 0) == 0 else { return 1e9 }
        return Kernel.wall() - (Double(boot.tv_sec) + Double(boot.tv_usec) / 1e6)
    }

    /// janitor.battery(): (on battery, percent); a Mac without one is (false, 100)
    static func battery() -> (Bool, Int) {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let list = IOPSCopyPowerSourcesList(info)?.takeRetainedValue() as? [CFTypeRef]
        else { return (false, 100) }
        for source in list {
            guard let d = IOPSGetPowerSourceDescription(info, source)?.takeUnretainedValue() as? [String: Any],
                  d[kIOPSTypeKey] as? String == kIOPSInternalBatteryType
            else { continue }
            let onBattery = d[kIOPSPowerSourceStateKey] as? String == kIOPSBatteryPowerValue
            let current = d[kIOPSCurrentCapacityKey] as? Int ?? 100
            let max = d[kIOPSMaxCapacityKey] as? Int ?? 100
            return (onBattery, max > 0 ? current * 100 / max : current)
        }
        return (false, 100)
    }
}
