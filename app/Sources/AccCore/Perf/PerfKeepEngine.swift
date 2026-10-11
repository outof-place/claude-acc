// perf keep on events: claude-acc's tweaks (perf.py) come back when one of their files changes or the
// network does, instead of launchd running every check every 5 minutes. Python keeps the knowledge and
// the writes: `perf keep-spec` says what to watch (each applied tweak's class and files), and
// `perf keep --only a,b` repairs just those. A full `perf keep` (deferred undos, Ultra, processes,
// Docker, repositories, claude-hooks-async) runs at start and every `periodic_s` (30 min).
// Safety tweaks and perf ones come back on the same events. The difference is Python's: a perf tweak
// someone keeps reverting waits an hour, a safety one never does.
// A repair writes files it watches. Those events land while the run is on, and one more run confirms
// them: it finds nothing to change and writes nothing. Nothing polls between events.
import Darwin
import Dispatch
import Synchronization
import notify

public final class PerfKeepEngine: @unchecked Sendable {
    public struct Options: Sendable {
        public var home: String
        public var python: [String]
        /// events this close together go to one run
        public var debounce = 0.5
        /// a network change comes in bursts (interfaces, routes, DNS)
        public var networkSettle = 2.0
        /// the shortest periodic pass a spec may ask for (tests lower it)
        public var minPeriodic = 60.0
        /// without a spec (a perf.py before keep-spec), every check as often as launchd ran them
        public var fallbackPeriodic = 300.0

        public init(home: String, python: [String]? = nil) {
            self.home = home
            let state = GuardPaths.state(home)
            self.python = python ?? [state + "/python", state + "/acc.py"]
        }
    }

    struct Tweak: Equatable {
        var name: String
        var cls: String
        var paths: [String]
        var network: Bool
        /// its own interval (`every_s`, docker-idle's samples): a run of just the tweak this often
        var every: Double?
    }

    let options: Options
    let queue = DispatchQueue(label: "acc-cored.perf", qos: .utility)
    lazy var watch = PathWatch(queue: queue)
    var tweaks: [Tweak] = []
    /// perf.json and perf-state.json: a change there (apply, undo, a run) reloads the spec
    var config: [String] = []
    var periodic = 1800.0
    var pending: Set<String> = []
    var fullDue = false
    var specDue = false
    var running = false
    var stopped = false
    var debounceTimer: DispatchSourceTimer?
    var periodicTimer: DispatchSourceTimer?
    var periodicEvery = 0.0
    var everyTimer: DispatchSourceTimer?
    var everyInterval = 0.0
    var networkToken: Int32?
    public let runs = Atomic<Int>(0)
    public let fails = Atomic<Int>(0)
    var lastRun: [String] = []
    /// tests: the run in place of Python's, and the spec in place of `perf keep-spec`
    var runner: (@Sendable ([String]) -> Int32)?
    var specReader: (() -> PyObject?)?

    public init(_ options: Options) {
        self.options = options
    }

    public func start() {
        queue.async { [self] in
            // the watch lives in this engine: a strong reference back would keep both forever
            watch.changed = { [weak self] moved in self?.changed(moved) }
            loadSpec()
            armNetwork()
            fullDue = true  // as launchd's RunAtLoad
            kick(after: 0)
        }
    }

    public func stop() {
        queue.sync {
            stopped = true
            debounceTimer?.cancel()
            debounceTimer = nil
            periodicTimer?.cancel()
            periodicTimer = nil
            everyTimer?.cancel()
            everyTimer = nil
            watch.stop()
            if let networkToken { notify_cancel(networkToken) }
            networkToken = nil
        }
    }

    /// For ping: runs so far and failed ones. Atomics: a ping never waits behind a spec read.
    public func status() -> PyObject {
        PyObject([("runs", .int(runs.load(ordering: .relaxed))), ("fails", .int(fails.load(ordering: .relaxed)))])
    }

    // MARK: spec

    /// `perf keep-spec`: what to watch. A spec that can't be read keeps the last one; with none at
    /// all (a perf.py before keep-spec) the full pass runs at launchd's interval, so nothing comes
    /// back later than it did.
    func loadSpec() {
        guard let spec = specReader?() ?? Self.liveSpec(options) else {
            if tweaks.isEmpty { periodic = max(options.minPeriodic, options.fallbackPeriodic) }
            schedulePeriodic()
            return
        }
        periodic = max(options.minPeriodic, spec["periodic_s"]?.double ?? 1800)
        config = spec["config"]?.array?.compactMap(\.string) ?? []
        tweaks = (spec["tweaks"]?.array ?? []).compactMap { t in
            guard let name = t["name"]?.string else { return nil }
            return Tweak(name: name, cls: t["class"]?.string ?? "periodic", paths: t["paths"]?.array?.compactMap(\.string) ?? [],
                         network: t["network"]?.truthy ?? false, every: t["every_s"]?.double)
        }
        watch.set(Set(tweaks.filter { $0.cls != "periodic" }.flatMap(\.paths) + config))
        schedulePeriodic()
        scheduleEvery()
    }

    static func liveSpec(_ options: Options) -> PyObject? {
        // under launchd's ExitTimeOut (30 s): stop() waits for the queue, a spec read included
        guard let out = Spawn.output(options.python + ["perf", "keep-spec"], timeout: 20) else { return nil }
        return (try? PyJSON.loads(bytes: out))?.object
    }

    // MARK: triggers

    /// Watched paths whose stamp moved: the tweaks on them run, a config change reloads the spec.
    func changed(_ moved: [String]) {
        if moved.contains(where: config.contains) { specDue = true }
        for t in tweaks where t.cls != "periodic" && t.paths.contains(where: moved.contains) { pending.insert(t.name) }
        kick(after: options.debounce)
    }

    func armNetwork() {
        var token: Int32 = 0
        // kNotifySCNetworkChange (notify_keys.h): the default route, an interface or DNS changed
        let status = notify_register_dispatch("com.apple.system.config.network_change", &token, queue) { [weak self] _ in
            self?.networkChanged()
        }
        if status == 0 { networkToken = token }
    }

    func networkChanged() {
        for t in tweaks where t.network { pending.insert(t.name) }
        kick(after: options.networkSettle)
    }

    func schedulePeriodic() {
        guard !stopped, periodicTimer == nil || periodicEvery != periodic else { return }
        periodicTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(wallDeadline: .now() + periodic, repeating: periodic, leeway: .seconds(Int(min(60, periodic / 10))))
        t.setEventHandler { [weak self] in
            guard let self else { return }
            self.fullDue = true
            self.kick(after: 0)
        }
        t.resume()
        periodicTimer = t
        periodicEvery = periodic
    }

    /// One timer at the shortest own interval; each tick runs every tweak that has one.
    func scheduleEvery() {
        let interval = tweaks.compactMap(\.every).min().map { max(options.minPeriodic, $0) }
        guard let interval, !stopped else {
            everyTimer?.cancel()
            everyTimer = nil
            return
        }
        guard everyTimer == nil || everyInterval != interval else { return }
        everyTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(wallDeadline: .now() + interval, repeating: interval, leeway: .seconds(Int(min(60, interval / 10))))
        t.setEventHandler { [weak self] in
            guard let self else { return }
            for tweak in self.tweaks where tweak.every != nil { self.pending.insert(tweak.name) }
            self.kick(after: 0)
        }
        t.resume()
        everyTimer = t
        everyInterval = interval
    }

    // MARK: runs

    /// A run in `seconds`, unless one is on (its end looks again) or one is set already: a stream of
    /// events doesn't push it back.
    func kick(after seconds: Double) {
        guard !stopped, !running, debounceTimer == nil, fullDue || specDue || !pending.isEmpty else { return }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + max(0, seconds), leeway: .milliseconds(100))
        t.setEventHandler { [weak self] in self?.run() }
        t.resume()
        debounceTimer = t
    }

    func run() {
        debounceTimer?.cancel()
        debounceTimer = nil
        guard !stopped else { return }
        if specDue {
            specDue = false
            loadSpec()
        }
        // a spec change can drop a tweak that was waiting (undone meanwhile)
        let known = Set(tweaks.map(\.name))
        pending = pending.filter(known.contains)
        guard fullDue || !pending.isEmpty else { return }
        let full = fullDue
        let names = full ? [] : pending.sorted()
        fullDue = false
        pending = []
        running = true
        // the utility band: a safety repair shouldn't wait behind background throttling
        let argv = ["/usr/sbin/taskpolicy", "-c", "utility"] + options.python + ["perf", "keep"] + (full ? [] : ["--only", names.joined(separator: ",")])
        let log = GuardPaths.state(options.home) + "/perf-launchd.log"
        let runner = self.runner
        DispatchQueue.global(qos: .utility).async {
            let rc = runner?(argv) ?? Spawn.run(argv, log: log, timeout: 600)
            self.queue.async { self.finished(rc, full ? ["(all)"] : names) }
        }
    }

    /// The run's own writes came in as events meanwhile: they're pending, and the next run confirms them.
    func finished(_ rc: Int32, _ names: [String]) {
        running = false
        runs.add(1, ordering: .relaxed)
        if rc != 0 { fails.add(1, ordering: .relaxed) }
        lastRun = names
        kick(after: options.debounce)
    }
}
