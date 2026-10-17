import AccKit
import Foundation
import ServiceManagement

/// `$STATE/pod-services.json` while Pod owns claude-acc: the launchd agents setup.sh writes and this
/// app's login item, each with Background Task Management's status for it. Pod's Settings show the
/// file (acc-kit's AccServicesView). Pod registers no service itself: its runtime runs setup.sh,
/// which writes the plists to ~/Library/LaunchAgents, so this app, which runs from login, reports
/// them. BTM sends no event when a switch flips in System Settings › Login Items, so the minute poll
/// asks again (eight calls: ~0.3 ms of CPU and ~4 ms of waiting, off the main actor), and the file
/// is written when an answer changed, or every 15 minutes for the "checked" line.
final class ServicesReport {
    /// setup.sh's JOBS, in its order (a test reads them out of setup.sh).
    static let jobs = [
        "com.filip.claude-acc", "com.filip.claude-acc.janitor", "com.filip.claude-acc.devguard",
        "com.filip.claude-acc.perf", "com.filip.claude-acc.updates", "com.filip.claude-acc.jobs",
        "com.filip.claude-acc.hotspot-user",
    ]
    /// This app's login item (SMAppService.mainApp), by its bundle id.
    static let loginItem = "com.filip.claude-acc.menubar"
    /// A file whose answers didn't change is still rewritten this often, so "checked" stays true.
    static let rewriteEvery: TimeInterval = 15 * 60

    nonisolated struct Entry: Equatable, Sendable {
        let kind: String
        let name: String
        let status: String
    }

    private let paths: AccPaths
    private let agents: URL
    private let legacyStatus: @Sendable (URL) -> SMAppService.Status
    private let loginItemStatus: @Sendable () -> SMAppService.Status
    private var last: [Entry]?
    private var writtenAt = Date.distantPast
    private var checking = false

    /// HOME, not the account's home: the measure harness runs this app under CFFIXED_USER_HOME, and
    /// CLI.directory follows HOME too.
    init(
        home: URL = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true),
        legacyStatus: @escaping @Sendable (URL) -> SMAppService.Status = { SMAppService.statusForLegacyPlist(at: $0) },
        loginItemStatus: @escaping @Sendable () -> SMAppService.Status = { SMAppService.mainApp.status }
    ) {
        paths = AccPaths(home: home)
        agents = home.appending(path: "Library/LaunchAgents", directoryHint: .isDirectory)
        self.legacyStatus = legacyStatus
        self.loginItemStatus = loginItemStatus
    }

    /// Asks BTM and writes the file when Pod owns claude-acc; leaves it alone otherwise (a brew or
    /// turned-off install shows no services, and the file would only go stale).
    func refresh(now: Date = .now) async {
        guard !checking, let owner = readOwner(), owner.isPod else { return }
        checking = true
        defer { checking = false }
        let urls = Self.jobs.map { agents.appending(path: $0 + ".plist", directoryHint: .notDirectory) }
        let (legacy, login, loginName) = (legacyStatus, loginItemStatus, Self.loginItem)
        let entries = await Task.detached {
            urls.map { Entry(kind: "agent", name: $0.lastPathComponent, status: Self.word(legacy($0))) }
                + [Entry(kind: "login-item", name: loginName, status: Self.word(login()))]
        }.value
        guard entries != last || now.timeIntervalSince(writtenAt) >= Self.rewriteEvery else { return }
        if write(entries, owner: owner, now: now) {
            last = entries
            writtenAt = now
        }
    }

    /// SMAppService.Status as AccKit's AccPodServices reads it. A plist that isn't there reads
    /// not-registered (measured on macOS 27), not not-found.
    nonisolated static func word(_ status: SMAppService.Status) -> String {
        switch status {
        case .enabled: "enabled"
        case .requiresApproval: "requires-approval"
        case .notRegistered: "not-registered"
        case .notFound: "not-found"
        @unknown default: "not-found"
        }
    }

    private func readOwner() -> AccOwner? {
        guard let data = try? Data(contentsOf: paths.owner) else { return nil }
        return try? JSONDecoder().decode(AccOwner.self, from: data)
    }

    /// One atomic write (a temp file, then a rename): AccStore's watch never reads half of it.
    private func write(_ entries: [Entry], owner: AccOwner, now: Date) -> Bool {
        let body: [String: Any] = [
            "version": 1,
            "at": now.formatted(.iso8601),
            "app": owner.app ?? Bundle.main.bundlePath,
            "payload": owner.version.map { $0 as Any } ?? NSNull(),
            "services": entries.map { ["kind": $0.kind, "name": $0.name, "status": $0.status] },
            // pod-rootd belongs to Pod's own registration, which no Pod ships yet: an empty list
            // offers no root helper (AccServicesView.offersRootHelper)
            "daemons": [[String: String]](),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: body, options: [.prettyPrinted, .sortedKeys])
        else { return false }
        do {
            try data.write(to: paths.podServices, options: .atomic)
            return true
        } catch {
            return false
        }
    }
}
