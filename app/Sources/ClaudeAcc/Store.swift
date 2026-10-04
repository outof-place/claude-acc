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
    private(set) var problem: String?
    private(set) var busy: Busy?
    var notice: Notice?
    private(set) var launchAtLogin = false
    private(set) var janitor: JanitorState?
    private(set) var disk: DiskSpace?
    private(set) var sweeping = false
    private(set) var guardState: GuardState?
    /// Unit the panel is restarting or stopping right now.
    private(set) var guardBusy: String?
    /// The mode just picked in the panel, until the guard's next snapshot shows it.
    private var guardModeOverride: String?
    /// Rendering only: the account whose details start open.
    @ObservationIgnored var previewOpenAccount: String?

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
    init(preview: Snapshot, guardState: GuardState? = nil, janitor: JanitorState? = nil) {
        snapshot = preview
        readLocal()
        if let guardState { self.guardState = guardState }
        if let janitor { self.janitor = janitor }
    }

    init() {
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
        let age = Date.now.timeIntervalSince1970 - (snapshot?.generatedAt ?? 0)
        if age > 30 { Task { await refresh() } }
        live?.cancel()
        live = Task { [weak self] in
            while !Task.isCancelled {
                self?.readLocal()
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    func panelDisappeared() {
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
