import AppKit
import Darwin

/// `ClaudeAcc --measure-panel <seconds> [--store-only | --closed] [--trace-wakeups]`: what the open
/// panel costs, with nothing on the user's screen. Run under CFFIXED_USER_HOME pointing at a copy of
/// the state files (scripts/measure_panel.py replays the real ones there at their tempo), the store
/// reads them as the live app does and the panel lives in a window off every display, drawn at the
/// display's scale. After a 10 s warm-up and `seconds` one line of JSON gives CPU time per second of
/// the app, its main thread and the scripts it ran. No login item, no Stay Awake assertion, no
/// dictation, no ring. `--store-only` leaves the panel out (the difference is SwiftUI's); `--closed`
/// keeps it hosted but closed, as it waits between clicks.
final class PanelMeasure {
    private static let warmup: TimeInterval = 10
    private static let offscreen = NSPoint(x: -30_000, y: -30_000)

    private let store = Store(sideEffects: false)
    private var window: PanelWindow?
    private let mode: String
    private let seconds: Double
    private var observer: CFRunLoopObserver?
    private var wokeAt: UInt64 = 0
    private var busy: UInt64 = 0
    private var wakeups = 0
    private var first: Sample?
    /// `--trace-wakeups`: when each of the first 400 measured wakeups came and how long it ran.
    private let tracing = CommandLine.arguments.contains("--trace-wakeups")
    private var trace: [(at: UInt64, busy: UInt64)] = []

    static func requested(_ args: [String]) -> Bool { args.contains("--measure-panel") }

    init?(arguments args: [String]) {
        guard let i = args.firstIndex(of: "--measure-panel"), i + 1 < args.count,
              let seconds = Double(args[i + 1]), seconds > 0
        else { return nil }
        self.seconds = seconds
        mode = args.contains("--store-only") ? "store-only" : args.contains("--closed") ? "closed" : "open"
        NSApp.setActivationPolicy(.accessory)
        if mode != "store-only" {
            let window = PanelWindow()
            window.setContent(PanelView(store: store)) { [weak window] size in
                guard let window, size.width > 0 else { return }
                window.setFrame(
                    PanelWindow.frame(forCard: NSRect(origin: Self.offscreen, size: size)), display: false)
            }
            window.setFrame(
                PanelWindow.frame(forCard: NSRect(origin: Self.offscreen, size: window.contentSize)), display: false)
            window.showOffscreen(at: Self.offscreen)
            self.window = window
        }
        if mode != "closed" { store.panelAppeared() }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.warmup) { [self] in begin() }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.warmup + seconds) { [self] in finish() }
    }

    private func begin() {
        let observer = CFRunLoopObserverCreateWithHandler(
            nil, CFRunLoopActivity.afterWaiting.rawValue | CFRunLoopActivity.beforeWaiting.rawValue, true, 0
        ) { [unowned self] _, activity in
            MainActor.assumeIsolated {
                let now = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
                if activity == .afterWaiting {
                    wakeups += 1
                    wokeAt = now
                } else if wokeAt > 0 {
                    busy += now - wokeAt
                    if tracing, trace.count < 400 { trace.append((wokeAt, now - wokeAt)) }
                    wokeAt = 0
                }
            }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        self.observer = observer
        // what a view body prints (Self._printChanges() in a measuring build) counts from here
        print("measure: begin")
        first = Sample()
    }

    private func finish() {
        guard let first else { return }
        let last = Sample()
        let wall = Double(last.wall - first.wall) / 1e9
        let perSecond = { (ns: UInt64) in (Double(ns) / 1e6 / wall * 1000).rounded() / 1000 }
        let result: [String: Any] = [
            "mode": mode,
            "seconds": (wall * 10).rounded() / 10,
            "appMsPerS": perSecond(last.app - first.app),
            "mainThreadMsPerS": perSecond(last.main - first.main),
            "mainBusyMsPerS": perSecond(busy),
            "runLoopWakeupsPerS": (Double(wakeups) / wall * 100).rounded() / 100,
            "wakeupsPerS": (Double(last.wakeups - first.wakeups) / wall * 100).rounded() / 100,
            "childrenMsPerS": perSecond(last.children - first.children),
            "footprintMB": (Double(last.footprint) / 1_048_576).rounded(),
            // drawn at the scale a display would ask for, and whether AppKit counts it as visible
            "panel": window.map {
                [
                    "width": $0.frame.width, "height": $0.frame.height, "x": $0.frame.minX,
                    "scale": $0.backingScaleFactor, "screen": $0.screen != nil,
                    "visible": $0.occlusionState.contains(.visible),
                ]
            } as Any,
        ]
        let data = (try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])) ?? Data()
        for wake in trace {
            print("wake \(Double(wake.at - first.wall) / 1e6) \(Double(wake.busy) / 1e3)")
        }
        // print's buffer first: the JSON stays the last line
        fflush(stdout)
        FileHandle.standardOutput.write(data + Data("\n".utf8))
        exit(0)
    }

    /// Clocks and counters at one moment, in nanoseconds where they are times.
    private struct Sample {
        let wall = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
        /// Called on the main thread, so this is the main thread's own CPU time.
        let main = clock_gettime_nsec_np(CLOCK_THREAD_CPUTIME_ID)
        let app: UInt64
        let children: UInt64
        let wakeups: UInt64
        let footprint: UInt64

        init() {
            app = Self.cpu(RUSAGE_SELF)
            children = Self.cpu(RUSAGE_CHILDREN)
            var info = rusage_info_v4()
            let status = withUnsafeMutablePointer(to: &info) {
                $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                    proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0)
                }
            }
            wakeups = status == 0 ? info.ri_interrupt_wkups + info.ri_pkg_idle_wkups : 0
            footprint = status == 0 ? info.ri_phys_footprint : 0
        }

        private static func cpu(_ who: Int32) -> UInt64 {
            var usage = rusage()
            getrusage(who, &usage)
            let micros = { (t: timeval) in UInt64(t.tv_sec) * 1_000_000 + UInt64(t.tv_usec) }
            return (micros(usage.ru_utime) + micros(usage.ru_stime)) * 1000
        }
    }
}
