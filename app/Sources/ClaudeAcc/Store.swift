import AppKit
import Observation
import ServiceManagement

/// App state: the last readings and the action in progress.
@Observable
final class Store {
    enum Busy: Equatable {
        case switching(String)
        case loggingIn(String)
    }

    struct Notice: Equatable {
        let text: String
        var isError = false
    }

    private(set) var snapshot: Snapshot?
    private(set) var refreshing = false
    /// The panel window is on screen. Closed, it stays alive offscreen and would keep rendering
    /// every frame of a running animation, so animations and clocks follow this.
    private(set) var panelOpen = false
    private(set) var problem: String?
    private(set) var busy: Busy?
    /// `claude-acc resume` is running: the pause notice keeps its button disabled.
    private(set) var resuming = false
    var notice: Notice?
    private(set) var launchAtLogin = false
    private(set) var janitor: JanitorState?
    private(set) var disk: DiskSpace?
    private(set) var sweeping = false
    private(set) var updates: UpdatesState?
    private(set) var updating = false
    private(set) var guardState: GuardState?
    /// Unit the panel is restarting or stopping right now.
    private(set) var guardBusy: String?
    private(set) var fanState: FanState?
    private(set) var ultra: Ultra?
    /// The Go build scheduler: running, queued, memory split, today.
    private(set) var sched: SchedState?
    /// kern.memorystatus_level, for the Builds memory lane while the scheduler is idle.
    private(set) var memoryLevel: Double?
    /// CPU and GPU load since the previous reading.
    private(set) var load: LoadReading?
    @ObservationIgnored private let loadSampler = LoadSampler()
    /// Turning Ultra on or off measures as it goes, which takes a while.
    private(set) var ultraBusy = false
    /// The state just asked for, until perf.py is done.
    private(set) var ultraPick: Bool?
    /// The fan mode just picked, until the daemon's state file shows it.
    private(set) var fanPick: String?
    /// The mode just picked in the panel, until the guard's next snapshot shows it.
    private var guardModeOverride: String?
    /// Rendering only: the account whose details start open.
    @ObservationIgnored var previewOpenAccount: String?
    /// Rendering only: the account drawn as if the pointer were over it.
    @ObservationIgnored var previewHoverAccount: String?
    /// Stay Awake lives as long as the app: power assertions and the hotspot watch.
    let awake: Awake

    @ObservationIgnored private var loginPID: Int32?
    @ObservationIgnored private var loginCancelled = false
    @ObservationIgnored private var refreshAgain = false
    @ObservationIgnored private var poller: Task<Void, Never>?
    @ObservationIgnored private var live: Task<Void, Never>?

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    /// Rendering the panel to a file: fixed data, no timers, no login item.
    init(
        preview: Snapshot, guardState: GuardState? = nil, janitor: JanitorState? = nil, fans: FanState? = nil,
        ultra: Ultra? = nil, load: LoadReading? = nil, sched: SchedState? = nil, updates: UpdatesState? = nil
    ) {
        awake = Awake(preview: true)
        snapshot = preview
        readLocal()
        if let guardState { self.guardState = guardState }
        if let janitor { self.janitor = janitor }
        if let fans { fanState = fans }
        if let ultra { self.ultra = ultra }
        if let load { self.load = load }
        if let sched { self.sched = sched }
        if let updates { self.updates = updates }
    }

    init() {
        awake = Awake()
        // quitting during a sign-in must not leave the script with `claude` behind
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.cancelLogin() }
        }
        registerLoginItemOnFirstRun()
        launchAtLogin = SMAppService.mainApp.status == .enabled
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                // a baseline, so the panel opens with the last minute's load instead of a dash
                self?.sampleLoad()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) -> T? {
        try? decoder.decode(type, from: data)
    }

    // MARK: Reading

    func refresh() async {
        if refreshing {
            refreshAgain = true  // e.g. after a switch: the reading in flight already has old data
            return
        }
        refreshing = true
        defer { refreshing = false }
        readLocal()
        repeat {
            refreshAgain = false
            let result = await CLI.run(["status", "--json"])
            guard result.status == 0 else {
                problem = result.message.isEmpty ? "Couldn't read account limits" : result.message
                continue
            }
            do {
                snapshot = try Self.decoder.decode(Snapshot.self, from: Data(result.stdout.utf8))
                problem = nil
            } catch {
                problem = "The script returned unreadable data: \(error.localizedDescription)"
            }
        } while refreshAgain
    }

    /// Cleanup and guard state from their files, and free disk space: no script runs.
    func readLocal() {
        if let data = FileManager.default.contents(atPath: CLI.janitorState) {
            janitor = Self.decode(JanitorState.self, from: data)
        }
        if let data = FileManager.default.contents(atPath: CLI.fanState) {
            fanState = Self.decode(FanState.self, from: data)
            if let pick = fanPick, pick == fanMode { fanPick = nil }
        }
        if let data = FileManager.default.contents(atPath: CLI.schedState) {
            sched = Self.decode(SchedState.self, from: data)
        }
        if let data = FileManager.default.contents(atPath: CLI.updatesState) {
            updates = Self.decode(UpdatesState.self, from: data)
        }
        memoryLevel = Self.kernelMemoryLevel()
        if let data = FileManager.default.contents(atPath: CLI.perfState) {
            ultra = Self.decode(PerfFile.self, from: data)?.ultra
        }
        if let data = FileManager.default.contents(atPath: CLI.guardState) {
            guardState = Self.decode(GuardState.self, from: data)
            if guardModeOverride == guardState?.snapshot?.mode { guardModeOverride = nil }
        }
        let keys: Set<URLResourceKey> = [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]
        if let values = try? URL(fileURLWithPath: "/System/Volumes/Data").resourceValues(forKeys: keys),
           let free = values.volumeAvailableCapacityForImportantUsage, let total = values.volumeTotalCapacity, total > 0 {
            disk = DiskSpace(free: Double(free), total: Double(total))
        }
    }

    /// While the panel is open the guard's numbers move every few seconds.
    func panelAppeared() {
        panelOpen = true
        let age = Date.now.timeIntervalSince1970 - (snapshot?.generatedAt ?? 0)
        if age > 30 { Task { await refresh() } }
        live?.cancel()
        live = Task { [weak self] in
            while !Task.isCancelled {
                self?.readLocal()
                self?.sampleLoad()
                // the scheduler rewrites its state every second while builds run
                try? await Task.sleep(for: .seconds(self?.sched?.busy == true ? 1 : 3))
            }
        }
    }

    func panelDisappeared() {
        panelOpen = false
        live?.cancel()
        live = nil
    }

    // MARK: Accounts

    func switchTo(_ account: Account) async {
        guard busy == nil else { return }
        busy = .switching(account.email)
        notice = nil
        let result = await CLI.run(["switch", account.email])
        busy = nil
        notice = result.status == 0
            ? Notice(text: "Switched to \(account.email)")
            : Notice(text: result.message, isError: true)
        await refresh()
    }

    func login(_ account: Account) async {
        guard busy == nil else { return }
        loginCancelled = false
        busy = .loggingIn(account.email)
        notice = nil
        let result = await CLI.run(CLI.process(["login", account.email])) { [weak self] pid in
            Task { @MainActor in self?.loginPID = pid }
        }
        loginPID = nil
        busy = nil
        // back from the browser the script ignores Cancel and finishes saving, so the result decides
        if result.status == 0 {
            notice = Notice(text: "Signed in \(account.email)")
        } else if loginCancelled {
            notice = Notice(text: "Sign-in cancelled")
        } else {
            notice = Notice(text: result.message.isEmpty ? "Sign-in failed" : result.message, isError: true)
        }
        await refresh()
    }

    func cancelLogin() {
        guard let pid = loginPID else { return }
        loginCancelled = true
        kill(pid, SIGTERM)  // on SIGTERM the script also closes `claude auth login`
    }

    /// Lifts the limit pause until limits recover: paused sessions wake up right away.
    func resumePaused() async {
        guard !resuming else { return }
        resuming = true
        notice = nil
        let result = await CLI.run(["resume"])
        resuming = false
        notice = result.status == 0
            ? Notice(text: "Pause lifted, sessions are resuming")
            : Notice(text: result.message.isEmpty ? "Couldn't lift the pause" : result.message, isError: true)
        await refresh()
    }

    // MARK: Cleanup

    /// Clean up now instead of waiting for launchd: every task, whatever its schedule.
    func sweep() async {
        guard !sweeping else { return }
        sweeping = true
        notice = nil
        let result = await CLI.run(["sweep", "--force"], script: CLI.janitor)
        sweeping = false
        readLocal()
        if result.status != 0 {
            notice = Notice(text: result.message.isEmpty ? "Cleanup failed" : result.message, isError: true)
        } else if result.message == "porządki już trwają" {
            notice = Notice(text: "A cleanup is already running")
        } else if let last = janitor?.lastSweep {
            notice = Notice(text: "Cleanup freed \(Format.bytes(last.freed))")
        }
    }

    // MARK: Updates

    /// Upgrade Homebrew, npm and Go packages now instead of waiting for the 4:30 run.
    func runUpdates() async {
        guard !updating else { return }
        updating = true
        notice = nil
        let result = await CLI.run(["run", "--force"], script: CLI.updates)
        updating = false
        readLocal()
        if result.status != 0 {
            notice = Notice(text: result.message.isEmpty ? "Update failed" : result.message, isError: true)
        } else if result.message == "aktualizacja już trwa" {
            notice = Notice(text: "An update is already running")
        } else if let run = updates?.lastRun {
            let packages = { (n: Int) in n == 1 ? "1 package" : "\(n) packages" }
            notice = run.ok
                ? Notice(text: run.updated == 0 ? "Everything was already up to date" : "Updated \(packages(run.updated))")
                : Notice(text: "Updated \(packages(run.updated)), \(run.failed) failed: see the Updates card", isError: true)
        }
    }

    /// Lists the projects Spotlight indexes and opens the pane that excludes them.
    func openSpotlightSettings() async {
        _ = await CLI.run(["spotlight"], script: CLI.janitor)
    }

    // MARK: Dev server guard

    var guardEnforcing: Bool { (guardModeOverride ?? guardState?.snapshot?.mode) != "observe" }

    /// Auto-manage on or off. The guard reads its config every pass, so this takes effect in seconds.
    func setGuardEnforcing(_ on: Bool) {
        let url = URL(fileURLWithPath: CLI.guardConfig)
        var config = (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any] ?? [:]
        let mode = on ? "enforce" : "observe"
        config["mode"] = mode
        do {
            let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: url, options: .atomic)
            guardModeOverride = mode
        } catch {
            notice = Notice(text: "Couldn't save the guard mode: \(error.localizedDescription)", isError: true)
        }
    }

    func restart(_ unit: GuardUnit) async { await guardCommand("recycle", unit, done: "Restarted", failed: "restart") }

    func stop(_ unit: GuardUnit) async { await guardCommand("stop", unit, done: "Stopped", failed: "stop") }

    private func guardCommand(_ command: String, _ unit: GuardUnit, done: String, failed: String) async {
        guard guardBusy == nil else { return }
        guardBusy = unit.key
        notice = nil
        let target = unit.ports.first.map { ":\($0)" } ?? String(unit.root)
        let result = await CLI.run([command, target], script: CLI.devguard)
        guardBusy = nil
        readLocal()
        let label = "\(unit.title) \(unit.portLabel)"
        let ok = result.status == 0 && guardState?.events?.last?.ok == true
        notice = ok
            ? Notice(text: "\(done) \(label)")
            : Notice(text: "Couldn't \(failed) \(label)", isError: true)
    }

    func openInBrowser(_ unit: GuardUnit) {
        guard let port = unit.ports.first, let url = URL(string: "http://localhost:\(port)") else { return }
        NSWorkspace.shared.open(url)
    }

    func copyCommand(_ unit: GuardUnit) {
        guard let command = unit.command else { return }
        let path = unit.launchCwd ?? unit.cwd.first ?? "~"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("cd \(path) && \(command)", forType: .string)
        notice = Notice(text: "Copied the command that starts \(unit.title)")
    }

    // MARK: Fans

    /// The daemon writes its state every 2 seconds; older than that by a margin means it isn't running.
    var fanDaemonRunning: Bool {
        guard let at = fanState?.at else { return false }
        return Date.now.timeIntervalSince1970 - at < 15
    }

    /// "auto", "50", "75", "100", or nil when nothing was picked yet.
    var fanMode: String? {
        if let pick = fanPick { return pick }
        guard let state = fanState else { return nil }
        if state.mode == "fixed", let percent = state.percent { return String(percent) }
        return state.mode
    }

    /// The root daemon reads this file every 2 seconds; it only accepts auto or 30-100%.
    func setFanMode(_ mode: String) {
        let config: [String: Any] = mode == "auto" ? ["mode": "auto"] : ["mode": "fixed", "percent": Int(mode) ?? 100]
        do {
            let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: URL(fileURLWithPath: CLI.fanConfig), options: .atomic)
            fanPick = mode
        } catch {
            notice = Notice(text: "Couldn't save the fan mode: \(error.localizedDescription)", isError: true)
        }
    }

    static func kernelMemoryLevel() -> Double? {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname("kern.memorystatus_level", &value, &size, nil, 0) == 0 ? Double(value) : nil
    }

    func sampleLoad() {
        if let reading = loadSampler.sample(), reading != load { load = reading }
    }

    // MARK: Ultra

    func setUltra(_ on: Bool) async {
        guard !ultraBusy else { return }
        ultraBusy = true
        ultraPick = on
        notice = nil
        let result = await CLI.run(["ultra", on ? "on" : "off"], script: CLI.perf)
        readLocal()
        ultraBusy = false
        ultraPick = nil
        notice = result.status == 0
            ? Notice(text: on ? "Ultra is on" : "Ultra is off, and everything it changed is back")
            : Notice(text: result.message, isError: true)
    }

    // MARK: Login item

    func setLaunchAtLogin(_ on: Bool) {
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            notice = Notice(text: "Couldn't change the login item: \(error.localizedDescription)", isError: true)
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    /// The app should run without being remembered, so the first run adds it to the login items.
    private func registerLoginItemOnFirstRun() {
        let key = "didRegisterLoginItem"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        try? SMAppService.mainApp.register()
        UserDefaults.standard.set(true, forKey: key)
    }
}
