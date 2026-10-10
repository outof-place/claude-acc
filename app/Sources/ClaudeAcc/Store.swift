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
    /// What the menu bar shows, assigned only when it changes: the label and its ring image
    /// re-render when the numbers do, not on every reading.
    private(set) var label = MenuLabelState()
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
    private(set) var mail: MailPanel?
    private(set) var mailChecking = false
    private(set) var browser: BrowserPanel?
    private(set) var browserBusy = false
    private(set) var desktop: DesktopPanel?
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
    private(set) var hotspot: HotspotState?
    /// The hotspot switch just flipped, until the daemon's state file shows it.
    private(set) var hotspotPick: Bool?
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
    /// Dictation lives as long as the app too: the right ⌘ tap and the widget.
    let dictation: Dictation

    @ObservationIgnored private var loginPID: Int32?
    @ObservationIgnored private var loginCancelled = false
    @ObservationIgnored private var refreshAgain = false
    @ObservationIgnored private var poller: Task<Void, Never>?
    @ObservationIgnored private var depotSyncing = false
    @ObservationIgnored private var browserSyncing = false
    @ObservationIgnored private var browserSyncedAt = Date.distantPast
    @ObservationIgnored private var desktopSyncing = false
    @ObservationIgnored private var desktopSyncedAt = Date.distantPast
    @ObservationIgnored private var live: Task<Void, Never>?
    /// The state files as last read: a file that didn't change costs one stat and wakes no view.
    @ObservationIgnored private var files: [String: StateFile] = [:]
    /// The newest readings while the panel is closed. The closed panel is still a live view
    /// tree, and publishing to it laid the whole of it out every minute for nothing (140 ms,
    /// sampled); it gets them when it opens. The menu bar label reads `label`, kept current.
    @ObservationIgnored private var latestSnapshot: Snapshot?
    @ObservationIgnored private var latestFan: FanState?
    @ObservationIgnored private var latestGuard: GuardState?
    /// A panel render: everything is published at once.
    @ObservationIgnored private var isPreview = false
    @ObservationIgnored private var diskCheckedAt = Date.distantPast
    @ObservationIgnored private var diskChecking = false

    private static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()

    /// Rendering the panel to a file: fixed data, no timers, no login item.
    init(
        preview: Snapshot, guardState: GuardState? = nil, janitor: JanitorState? = nil, fans: FanState? = nil,
        ultra: Ultra? = nil, load: LoadReading? = nil, sched: SchedState? = nil, depot: DepotRuns? = nil,
        updates: UpdatesState? = nil, desktop: DesktopPanel? = nil, link: TetherLink? = nil
    ) {
        awake = Awake(preview: true, link: link)
        dictation = Dictation(preview: true)
        isPreview = true
        snapshot = preview
        latestSnapshot = preview
        HostApp.current = preview.host ?? .orca
        readLocal()
        if let guardState { self.guardState = guardState }
        if let janitor { self.janitor = janitor }
        if let fans { fanState = fans }
        if let ultra { self.ultra = ultra }
        if let load { self.load = load }
        if let sched { self.sched = sched }
        if let depot { self.depot = depot }
        if let updates { self.updates = updates }
        if let desktop { self.desktop = desktop }
    }

    init() {
        awake = Awake()
        dictation = Dictation()
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
                await self?.poll()
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

    /// What `status --json` prints, written by every tick (launchd runs one every 2 minutes).
    private static let tickSnapshot = CLI.directory + "/status.json"
    /// A tick snapshot older than this means the ticks stopped: the script runs itself again.
    private static let tickSnapshotMaxAge: TimeInterval = 180

    /// The minute's reading. With the panel closed the tick's file stands in for the script,
    /// whose every run spawns ~20 Keychain reads; open, the panel asks the script as before.
    private func poll() async {
        if !panelOpen, readTickSnapshot() {
            readLocal()
            return
        }
        await refresh()
    }

    /// Takes the tick's snapshot when it's newer than the one shown. False when the script
    /// should run: no file, unreadable, or what's shown is older than `tickSnapshotMaxAge`.
    private func readTickSnapshot() -> Bool {
        if let data = changedFile(Self.tickSnapshot), let fresh = Self.decode(Snapshot.self, from: data),
           fresh.generatedAt > (latestSnapshot?.generatedAt ?? 0) {
            show(fresh)
        }
        return Date.now.timeIntervalSince1970 - (latestSnapshot?.generatedAt ?? 0) <= Self.tickSnapshotMaxAge
    }

    private func show(_ fresh: Snapshot) {
        latestSnapshot = fresh
        HostApp.current = fresh.host ?? .orca
        // with the panel closed only the menu bar label reads it; the panel takes it when it opens
        if panelOpen || isPreview || snapshot == nil { snapshot = fresh }
        if problem != nil { problem = nil }
        updateLabel()
        for (setting, pick) in settingPicks where pick == value(of: setting) { settingPicks[setting] = nil }
    }

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
                updateLabel()
                continue
            }
            do {
                show(try Self.decoder.decode(Snapshot.self, from: Data(result.stdout.utf8)))
            } catch {
                problem = "The script returned unreadable data: \(error.localizedDescription)"
                updateLabel()
            }
        } while refreshAgain
    }

    /// Cleanup and guard state from their files, and free disk space: no script runs.
    /// Observation tells every view that reads a property about every assignment, equal or
    /// not, so only what changed is assigned: an unchanged file isn't even read again. With the
    /// panel closed only what the menu bar label needs is read, and kept back from the panel.
    func readLocal() {
        let live = panelOpen || isPreview
        if RootHelper.shared.owns {
            // Pod's root helper drives the fans; the readings come from the SMC in this app
            if let fresh = RootHelper.shared.fanState, fresh.at != latestFan?.at {
                latestFan = fresh
                if live { fanState = latestFan }
            }
        } else if let data = changedFile(CLI.fanState) {
            latestFan = Self.decode(FanState.self, from: data)
            if live { fanState = latestFan }
        }
        if let data = changedFile(CLI.guardState) {
            latestGuard = Self.decode(GuardState.self, from: data)
            if live { guardState = latestGuard }
        }
        updateLabel()
        guard live else { return }
        if let data = changedFile(CLI.janitorState) {
            janitor = Self.decode(JanitorState.self, from: data)
        }
        if let pick = fanPick, pick == fanMode { fanPick = nil }
        if let data = changedFile(CLI.hotspotState) {
            hotspot = Self.decode(HotspotState.self, from: data)
        }
        if let pick = hotspotPick, pick == hotspot?.enabled { hotspotPick = nil }
        if let data = changedFile(CLI.hotspotConfig) {
            let enabled = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["enabled"] as? Bool ?? false
            if enabled != hotspotConfigEnabled { hotspotConfigEnabled = enabled }
        }
        // in Pod the controller is the user's agent and the root helper sets the limit
        let installed = FileManager.default.fileExists(atPath: CLI.hotspotDaemon)
            || (PodMenu.active && FileManager.default.isExecutableFile(atPath: CLI.directory + "/pod-rootctl"))
        if installed != hotspotInstalled { hotspotInstalled = installed }
        if let data = changedFile(CLI.schedState) {
            sched = Self.decode(SchedState.self, from: data)
        }
        if let data = changedFile(CLI.depotState) {
            depot = Self.decode(DepotRuns.self, from: data)
        }
        if let data = changedFile(CLI.updatesState) {
            updates = Self.decode(UpdatesState.self, from: data)
        }
        if let data = changedFile(CLI.mailPanel) {
            mail = Self.decode(MailPanel.self, from: data)
        }
        if let data = changedFile(CLI.browserPanel) {
            browser = Self.decode(BrowserPanel.self, from: data)
        }
        if let data = changedFile(CLI.desktopPanel) {
            desktop = Self.decode(DesktopPanel.self, from: data)
        }
        let level = Self.kernelMemoryLevel()
        if level != memoryLevel { memoryLevel = level }
        if let data = changedFile(CLI.perfState) {
            ultra = Self.decode(PerfFile.self, from: data)?.ultra
        }
        if let mode = guardModeOverride, mode == guardState?.snapshot?.mode { guardModeOverride = nil }
        refreshDisk()
    }

    /// Free space for important use is what Finder shows, and asking for it takes 15-58 ms
    /// (CacheDeleteCopyAvailableSpaceForVolume): off the main actor, at most once a minute,
    /// or now when `force` (the panel opened, a cleanup ran).
    func refreshDisk(force: Bool = false) {
        guard !diskChecking, force || Date.now.timeIntervalSince(diskCheckedAt) > 60 else { return }
        if isPreview {
            if let (free, total) = Self.readDiskSpace() { disk = DiskSpace(free: free, total: total) }
            return
        }
        diskChecking = true
        Task { [weak self] in
            let reading = await Self.diskSpace()
            guard let self else { return }
            self.diskChecking = false
            self.diskCheckedAt = .now
            guard let (free, total) = reading else { return }
            let space = DiskSpace(free: free, total: total)
            // free space moves by the byte; the card shows tenths of a gigabyte
            if self.disk?.total != space.total || self.disk.map({ Format.bytes($0.free) }) != Format.bytes(space.free) {
                self.disk = space
            }
        }
    }

    @concurrent
    private static func diskSpace() async -> (free: Double, total: Double)? { readDiskSpace() }

    nonisolated private static func readDiskSpace() -> (free: Double, total: Double)? {
        let keys: Set<URLResourceKey> = [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]
        guard let values = try? URL(fileURLWithPath: "/System/Volumes/Data").resourceValues(forKeys: keys),
              let free = values.volumeAvailableCapacityForImportantUsage, let total = values.volumeTotalCapacity, total > 0
        else { return nil }
        return (Double(free), Double(total))
    }

    /// The menu bar's numbers from the newest readings; assigned only when they change.
    private func updateLabel() {
        let used = latestSnapshot?.active?.worstUsed
        let text: String = if let snapshot = latestSnapshot {
            snapshot.foreignRuntime ? "?" : Format.percent(used)
        } else {
            problem == nil ? "…" : "!"
        }
        var hot: Int?
        if let fan = latestFan, Date.now.timeIntervalSince1970 - fan.at < 15 {
            let value = [fan.cpu, fan.gpu].compactMap(\.self).max() ?? 0
            if value >= 90 { hot = Int(value.rounded()) }
        }
        let badge: MenuLabelState.Badge? = latestGuard?.snapshot?.pressure.level == 2
            ? .memory : latestSnapshot?.anyNeedsLogin == true ? .login : nil
        let fresh = MenuLabelState(used: used, text: text, badge: badge, hot: hot)
        if fresh != label { label = fresh }
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
        RootHelper.shared.panelOpen = true
        // what came in while it was closed
        if let latestSnapshot { snapshot = latestSnapshot }
        fanState = latestFan
        guardState = latestGuard
        refreshDisk(force: true)
        _ = readTickSnapshot()  // a tick that just ran saves the script run
        let age = Date.now.timeIntervalSince1970 - (snapshot?.generatedAt ?? 0)
        if age > 30 { Task { await refresh() } }
        live?.cancel()
        live = Task { [weak self] in
            while !Task.isCancelled {
                self?.readLocal()
                self?.sampleLoad()
                self?.syncDepot()
                self?.syncBrowser()
                self?.syncDesktop()
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

    /// The daemon rewrites the browser file on every change; a browser started, quit or switched
    /// on without an agent around shows up only through `status`, so it runs every 5 s while open.
    private func syncBrowser() {
        guard !browserSyncing, browser?.installed == true, Date.now.timeIntervalSince(browserSyncedAt) > 5 else { return }
        browserSyncing = true
        Task { [weak self] in
            _ = await CLI.run(["status", "--json"], script: CLI.browser)
            self?.browserSyncing = false
            self?.browserSyncedAt = .now
            self?.readLocal()
        }
    }

    /// Uprawnienia i wyświetlacze bramy pulpitu zmieniają się poza sesją agenta (przyznanie w
    /// Ustawieniach, podłączony ekran), więc odświeżamy przez `status` co 5 s, gdy panel otwarty.
    private func syncDesktop() {
        guard !desktopSyncing, desktop?.installed == true, Date.now.timeIntervalSince(desktopSyncedAt) > 5 else { return }
        desktopSyncing = true
        Task { [weak self] in
            _ = await CLI.run(["status", "--json"], script: CLI.desktop)
            self?.desktopSyncing = false
            self?.desktopSyncedAt = .now
            self?.readLocal()
        }
    }

    // MARK: Desktop gateway

    /// Otwiera panel Ustawień (Dostępność albo Nagrywanie ekranu), gdzie człowiek zaznacza binarkę pomocnika.
    func openDesktopPermissions() async {
        _ = await CLI.run(["doctor", "--open"], script: CLI.desktop)
        notice = Notice(text: "Opened System Settings: add the helper binary and tick it")
    }

    func panelDisappeared() {
        panelOpen = false
        RootHelper.shared.panelOpen = false
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
        refreshDisk(force: true)
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

    // MARK: Mail gateway

    /// Signs in to every mailbox once (mail.py doctor) and refreshes the card from its file.
    func checkMail() async {
        guard !mailChecking else { return }
        mailChecking = true
        notice = nil
        let result = await CLI.run(["doctor", "--quiet"], script: CLI.mail)
        mailChecking = false
        readLocal()
        let failing = mail?.mailboxes.filter { $0.health?.ok == false }.count ?? 0
        if result.status == 0 {
            notice = Notice(text: "Every mailbox answers")
        } else if failing > 0 {
            notice = Notice(text: failing == 1 ? "1 mailbox doesn't answer: see the Mail card" : "\(failing) mailboxes don't answer: see the Mail card", isError: true)
        } else {
            notice = Notice(text: result.message.isEmpty ? "Mail check failed" : result.message, isError: true)
        }
    }

    // MARK: Browser gateway

    /// Closes the agents' tabs and the connection (browser.py disconnect): the automation bar goes away.
    func disconnectBrowser() async {
        guard !browserBusy else { return }
        browserBusy = true
        let result = await CLI.run(["disconnect"], script: CLI.browser)
        browserBusy = false
        readLocal()
        notice = result.status == 0
            ? Notice(text: "Browser disconnected, agent tabs closed")
            : Notice(text: result.message.isEmpty ? "Disconnect failed" : result.message, isError: true)
    }

    /// Opens the browser's remote debugging page, where the user ticks the checkbox once.
    func enableBrowser(_ browser: BrowserPanel.Browser) async {
        let result = await CLI.run(["setup", browser.name], script: CLI.browser)
        notice = result.status == 0
            ? Notice(text: result.message.contains("schowku")
                ? "\(browser.inspect) copied: in \(browser.title) press ⌘L, ⌘V, Return, then tick Allow remote debugging"
                : "In \(browser.title), tick Allow remote debugging for this browser instance")
            : Notice(text: result.message.isEmpty ? "Couldn't open \(browser.title)" : result.message, isError: true)
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

    /// In Pod, through the root helper; otherwise the root daemon reads this file every 2 seconds and
    /// only accepts auto or 30-100%.
    func setFanMode(_ mode: String) {
        if RootHelper.shared.owns {
            fanPick = mode
            Task {
                if let problem = await RootHelper.shared.setFans(mode) {
                    notice = Notice(text: "Couldn't set the fans: \(problem)", isError: true)
                    fanPick = nil
                }
            }
            return
        }
        let config: [String: Any] = mode == "auto" ? ["mode": "auto"] : ["mode": "fixed", "percent": Int(mode) ?? 100]
        do {
            let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: URL(fileURLWithPath: CLI.fanConfig), options: .atomic)
            fanPick = mode
        } catch {
            notice = Notice(text: "Couldn't save the fan mode: \(error.localizedDescription)", isError: true)
        }
    }

    // MARK: Hotspot turbo

    /// Both read in `readLocal`, not in the card's body: a stat and a JSON parse on every render.
    private(set) var hotspotInstalled = false
    private(set) var hotspotConfigEnabled = false

    /// The daemon writes every 2 seconds; much older than that means it isn't running.
    var hotspotLive: HotspotState? {
        guard let state = hotspot, Date.now.timeIntervalSince1970 - state.at < 15 else { return nil }
        return state
    }

    var hotspotEnabled: Bool {
        if let pick = hotspotPick { return pick }
        if let state = hotspotLive { return state.enabled }
        return hotspotConfigEnabled
    }

    private static func hotspotConfig() -> [String: Any] {
        guard let data = FileManager.default.contents(atPath: CLI.hotspotConfig),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return object
    }

    /// The root daemon reads this file every 2 seconds; other keys (min_mbps, max_mbps) stay.
    func setHotspot(_ on: Bool) {
        var config = Self.hotspotConfig()
        config["enabled"] = on
        do {
            let data = try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: URL(fileURLWithPath: CLI.hotspotConfig), options: .atomic)
            hotspotPick = on
        } catch {
            notice = Notice(text: "Couldn't save the hotspot switch: \(error.localizedDescription)", isError: true)
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
        guard !PodMenu.active, !UserDefaults.standard.bool(forKey: Self.loginItemOffKey) else { return }
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
