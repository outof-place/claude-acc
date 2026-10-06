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
    /// Depot CI runs of the whole organization, also those started outside the scheduler.
    private(set) var depot: DepotRuns?
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
    /// The value asked of a watcher setting's command, shown until a reading confirms it.
    private(set) var settingPicks: [Setting: Bool] = [:]
    private(set) var settingBusy: Setting?
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
    @ObservationIgnored private var depotSyncing = false
    @ObservationIgnored private var live: Task<Void, Never>?
    /// The state files as last read: a file that didn't change costs one stat and wakes no view.
    @ObservationIgnored private var files: [String: StateFile] = [:]

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    /// Rendering the panel to a file: fixed data, no timers, no login item.
    init(
        preview: Snapshot, guardState: GuardState? = nil, janitor: JanitorState? = nil, fans: FanState? = nil,
        ultra: Ultra? = nil, load: LoadReading? = nil, sched: SchedState? = nil, depot: DepotRuns? = nil,
        updates: UpdatesState? = nil
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
        if let depot { self.depot = depot }
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
        registerLoginItemUnlessTurnedOff()
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
                for (setting, pick) in settingPicks where pick == value(of: setting) { settingPicks[setting] = nil }
            } catch {
                problem = "The script returned unreadable data: \(error.localizedDescription)"
            }
        } while refreshAgain
    }

    /// Cleanup and guard state from their files, and free disk space: no script runs.
    /// Observation tells every view that reads a property about every assignment, equal or
    /// not, so only what changed is assigned: an unchanged file isn't even read again.
    func readLocal() {
        if let data = changedFile(CLI.janitorState) {
            janitor = Self.decode(JanitorState.self, from: data)
        }
        if let data = changedFile(CLI.fanState) {
            fanState = Self.decode(FanState.self, from: data)
        }
        if let pick = fanPick, pick == fanMode { fanPick = nil }
        if let data = changedFile(CLI.schedState) {
            sched = Self.decode(SchedState.self, from: data)
        }
        if let data = changedFile(CLI.depotState) {
            depot = Self.decode(DepotRuns.self, from: data)
        }
        if let data = changedFile(CLI.updatesState) {
            updates = Self.decode(UpdatesState.self, from: data)
        }
        let level = Self.kernelMemoryLevel()
        if level != memoryLevel { memoryLevel = level }
        if let data = changedFile(CLI.perfState) {
            ultra = Self.decode(PerfFile.self, from: data)?.ultra
        }
        if let data = changedFile(CLI.guardState) {
            guardState = Self.decode(GuardState.self, from: data)
        }
        if let mode = guardModeOverride, mode == guardState?.snapshot?.mode { guardModeOverride = nil }
        let keys: Set<URLResourceKey> = [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]
        if let values = try? URL(fileURLWithPath: "/System/Volumes/Data").resourceValues(forKeys: keys),
           let free = values.volumeAvailableCapacityForImportantUsage, let total = values.volumeTotalCapacity, total > 0 {
            let space = DiskSpace(free: Double(free), total: Double(total))
            // free space moves by the byte; the card shows tenths of a gigabyte
            if disk?.total != space.total || disk.map({ Format.bytes($0.free) }) != Format.bytes(space.free) {
                disk = space
            }
        }
    }

    /// The file's bytes when they differ from the last read, nil when they don't or it's gone.
    /// The daemons replace their files by rename, so inode, mtime and size tell a new version
    /// apart without reading it; a rewrite with the same bytes is caught by comparing them.
    private func changedFile(_ path: String) -> Data? {
        var info = stat()
        guard stat(path, &info) == 0 else {
            files[path] = nil
            return nil
        }
        let stamp = StateFile.Stamp(info)
        if files[path]?.stamp == stamp { return nil }
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        defer { files[path] = StateFile(stamp: stamp, data: data) }
        return files[path]?.data == data ? nil : data
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
                self?.syncDepot()
                // the scheduler rewrites its state every second while builds run
                try? await Task.sleep(for: .seconds(self?.sched?.busy == true ? 1 : 3))
            }
        }
    }

    /// Asks Depot for the organization's runs: every 15 s while one runs, else every minute.
    /// The script skips the network when its file is that fresh, so two callers never double it.
    private func syncDepot() {
        guard !depotSyncing else { return }
        let maxAge = depot?.running.isEmpty == false ? 15 : 60
        if Date.now.timeIntervalSince1970 - (depot?.checkedAt ?? 0) < Double(maxAge) { return }
        depotSyncing = true
        Task { [weak self] in
            _ = await CLI.run(["depot", "--max-age", String(maxAge)], script: CLI.sched)
            self?.depotSyncing = false
            self?.readLocal()
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

    /// A watcher setting the panel switches through `claude-acc <command> on|off`.
    enum Setting: String {
        /// Turning it off during a pause wakes the paused sessions; the script rewrites the hooks.
        case limitPause = "pause"
        /// With no headroom anywhere, use up the last few percent of every account.
        case drain
    }

    func value(of setting: Setting) -> Bool? {
        switch setting {
        case .limitPause: snapshot?.limitPause
        case .drain: snapshot?.drain
        }
    }

    func set(_ setting: Setting, _ on: Bool) async {
        guard settingBusy == nil else { return }
        settingBusy = setting
        settingPicks[setting] = on
        notice = nil
        let result = await CLI.run([setting.rawValue, on ? "on" : "off"])
        settingBusy = nil
        if result.status == 0 {
            switch setting {
            case .limitPause:
                notice = Notice(text: on
                    ? "Limit pause is on: with no headroom left, sessions stop at a checkpoint"
                    : "Limit pause is off: sessions work until the limit and resume after the reset")
            case .drain:
                notice = Notice(text: on
                    ? "With no headroom left, sessions use up the last few percent of every account"
                    : "Accounts below the switch threshold stay untouched")
            }
        } else {
            settingPicks[setting] = nil
            notice = Notice(text: result.message.isEmpty ? "Couldn't change the setting" : result.message, isError: true)
        }
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

    /// Update Homebrew, npm, Go, Python and Claude Code now instead of waiting for the 4:30 run.
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
        UserDefaults.standard.set(!on, forKey: Self.loginItemOffKey)
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

    private static let loginItemOffKey = "launchAtLoginTurnedOff"

    /// The app should run without being remembered. Every install signs the bundle anew,
    /// and macOS can drop the login item with the old signature, so each launch puts it
    /// back unless the switch in the panel was turned off. `.requiresApproval` means it was
    /// turned off in System Settings, which stays the user's call.
    private func registerLoginItemUnlessTurnedOff() {
        guard !UserDefaults.standard.bool(forKey: Self.loginItemOffKey) else { return }
        switch SMAppService.mainApp.status {
        case .notRegistered, .notFound:
            try? SMAppService.mainApp.register()
        default:
            break
        }
    }
}

/// A state file as last read by the panel.
private struct StateFile {
    struct Stamp: Equatable {
        let inode: UInt64
        let size: Int64
        let seconds: Int
        let nanoseconds: Int

        init(_ info: stat) {
            inode = info.st_ino
            size = info.st_size
            seconds = info.st_mtimespec.tv_sec
            nanoseconds = info.st_mtimespec.tv_nsec
        }
    }

    let stamp: Stamp
    let data: Data
}
