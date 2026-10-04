import Foundation

let gigabyte = 1_073_741_824.0

// MARK: - Accounts (`claude-acc status --json`)

/// The script owns all account logic; the app only shows it and calls its commands.
struct Snapshot: Decodable {
    let generatedAt: Double
    let activeEmail: String?
    let foreignRuntime: Bool
    let thresholds: Thresholds
    let forecast: Forecast?
    let apiBackoffUntil: Double?
    let lastTick: Double?
    let switchedAt: Double?
    /// Account selected in Orca's menu. Auto-switch then stands still and switching is blocked.
    let orcaSelected: String?
    let accounts: [Account]

    var active: Account? { accounts.first { $0.active } }
    var others: [Account] { accounts.filter { !$0.active } }
    /// The account auto-switch moves to next.
    var next: Account? {
        others.compactMap { account in account.queue.map { (account, $0) } }.min { $0.1 < $1.1 }?.0
    }
    var anyNeedsLogin: Bool { accounts.contains { $0.status == .needsLogin } }
}

struct Thresholds: Decodable {
    let sessionLeft: Double
    let weeklyLeft: Double
}

struct Forecast: Decodable {
    let session: WindowForecast?
    let weekly: WindowForecast?
}

struct WindowForecast: Decodable {
    let rate: Double
    let atReset: Double
    let switchAt: Double?
}

struct UsageWindow: Decodable {
    let used: Double?
    let resetsAt: Double?
}

struct Account: Decodable, Identifiable {
    enum Status: String, Decodable {
        case ok
        case needsLogin = "needs_login"
        case error
    }

    let id: String
    let email: String
    let realEmail: String?
    let tier: String
    let active: Bool
    let lastResort: Bool
    let status: Status
    let note: String
    let usable: Bool
    let queue: Int?
    let session: UsageWindow?
    let weekly: UsageWindow?
    let dataAge: Int?
    /// Monthly anniversary of the subscription start: the API has no billing date.
    let renewsAt: Double?
    let subscriptionStatus: String?
    /// "2025-03-28": the day the subscription started, as the profile API reports it.
    let subscriptionSince: String?

    /// An Orca entry can hold a different account than its label says.
    var mislabeled: Bool { realEmail.map { $0.lowercased() != email.lowercased() } ?? false }
    /// The most used window: it is the one that stops work first.
    var worstUsed: Double? { [session?.used, weekly?.used].compactMap(\.self).max() }
}

// MARK: - Cleanup (`janitor-state.json`)

struct JanitorState: Decodable {
    struct Sweep: Decodable {
        let at: Double
        let freed: Double
        let items: Int
        let duration: Double
    }

    struct Alert: Decodable, Hashable {
        let kind: String
        let free: Double?
        let projects: [String]?
        let task: String?
        let error: String?
    }

    let lastSweep: Sweep?
    let freedTotal: Double?
    /// A sweep in progress; a crashed script can leave the mark, so only a fresh one counts.
    let runningSince: Double?
    let alerts: [Alert]?
}

struct DiskSpace {
    let free: Double
    let total: Double

    var used: Double { 1 - free / total }
}

// MARK: - Ultra (`perf-state.json`, written by perf.py)

/// The Ultra part of perf-state.json. Numbers come from perf.py; the words for them live
/// here, so the panel reads in English whatever the script's own messages say.
struct Ultra: Decodable {
    struct Result: Decodable {
        let before: Double?
        let after: Double?
        let unit: String?
    }

    struct Tweak {
        let title: String
        let detail: String
        let unit: String
        /// A setting rather than a measurement: before → after, never shown as a gain.
        var isSetting = false
    }

    struct Step {
        let title: String
        let detail: String
        let command: String?
    }

    let on: Bool
    let since: Double?
    let applied: [String]
    let pendingRoot: [String]
    let pendingManual: [String]
    let results: [String: Result]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        on = try c.decodeIfPresent(Bool.self, forKey: .on) ?? false
        since = try c.decodeIfPresent(Double.self, forKey: .since)
        applied = try c.decodeIfPresent([String].self, forKey: .applied) ?? []
        pendingRoot = try c.decodeIfPresent([String].self, forKey: .pendingRoot) ?? []
        pendingManual = try c.decodeIfPresent([String].self, forKey: .pendingManual) ?? []
        results = try c.decodeIfPresent([String: Result].self, forKey: .results) ?? [:]
    }

    private enum CodingKeys: String, CodingKey {
        case on, since, applied, pendingRoot, pendingManual, results
    }

    /// perf.py's ULTRA list, in the order it applies them.
    static let order = ["bg-helpers", "claude-hooks-async", "node-compile-cache", "devguard-budget", "docker-vm", "git-speed"]

    static let catalog: [String: Tweak] = [
        "bg-helpers": Tweak(
            title: "Background helpers", detail: "Helpers no agent waits on move to the efficiency cores",
            unit: "% of a P-core"),
        "claude-hooks-async": Tweak(
            title: "Async hooks", detail: "Memory hooks stop holding up every tool call", unit: "ms per tool call"),
        "node-compile-cache": Tweak(
            title: "Node compile cache", detail: "What sessions spawn (tsc, eslint, MCP servers) starts warm",
            unit: "ms to load TypeScript"),
        "devguard-budget": Tweak(
            title: "Dev server budget", detail: "The guard frees memory sooner", unit: "% of RAM", isSetting: true),
        "docker-vm": Tweak(
            title: "Docker VM", detail: "The VM gives back what containers don't use", unit: "GB", isSetting: true),
        "git-speed": Tweak(
            title: "Git", detail: "untrackedCache and fsmonitor in the repos you list", unit: "ms git status"),
    ]

    static let steps: [String: Step] = [
        "vnodes": Step(
            title: "Bigger file cache", detail: "The vnode cache is full, so every scan of node_modules starts cold. Needs root.",
            command: "claude-acc perf-root vnodes trial"),
        "shaper": Step(
            title: "Upload shaper", detail: "Uploads queue in the router. Keeping the queue on the Mac needs root.",
            command: "claude-acc perf-root trial"),
        "docker-quit": Step(title: "Quit Docker once", detail: "The new memory cap is written while Docker is closed.", command: nil),
        "docker-restart": Step(title: "Restart Docker", detail: "The new memory cap applies on its next start.", command: nil),
    ]

    static func number(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(value < 10 && value != value.rounded() ? 1 : 0))
            .locale(Locale(identifier: "en_US")))
    }
}

/// perf-state.json holds more (applied tweaks, benchmarks); the panel needs only Ultra.
struct PerfFile: Decodable {
    let ultra: Ultra?
}

// MARK: - Fans (`fans-state.json`, written by the root fanctl daemon)

struct FanState: Decodable {
    struct Fan: Decodable, Identifiable {
        let index: Int
        let rpm: Double
        let min: Double
        let max: Double
        let target: Double
        let manual: Bool

        var id: Int { index }
        var share: Double { max > 0 ? rpm / max : 0 }
    }

    let at: Double
    let fans: [Fan]
    let cpu: Double?
    let gpu: Double?
    /// nil until a mode is picked in the panel: the daemon keeps its hands off.
    let mode: String?
    let percent: Int?
    let boosting: Bool?
    let conflict: Bool?
    /// Hottest sensor per part: pcores, ecores, gpu, ssd, battery.
    let sensors: [String: Double]?
    /// `[time, cpu, gpu, rpm]` every 5 seconds, 20 minutes back.
    let history: [[Double]]?
    let error: String?

    var anyManual: Bool { fans.contains(where: \.manual) }
}

// MARK: - Dev server guard (`devguard-state.json`)

struct GuardState: Decodable {
    let snapshot: GuardSnapshot?
    let events: [GuardEvent]?
    /// `[time, dev servers, swap, compressed]` every 30 seconds, two hours back.
    let history: [[Double]]?
}

struct GuardSnapshot: Decodable {
    let at: Double
    let mode: String
    let pressure: MemoryPressure
    let budget: Double
    let total: Double
    let orca: Bool
    let units: [GuardUnit]
    let plans: [GuardPlan]

    func plan(for unit: GuardUnit) -> GuardPlan? { plans.first { $0.unit == unit.key } }
}

struct MemoryPressure: Decodable {
    let level: Int
    let notes: [String]?
    let available: Int?
    let swapUsed: Double
    let swapTotal: Double
    let swapping: Bool
    let compressed: Double
    let ram: Double
}

/// A command and the dev servers it started: one `next dev`, or a whole `pnpm dev` stack.
struct GuardUnit: Decodable, Identifiable {
    struct Client: Decodable {
        let pid: Int
        let kind: String
        let name: String
    }

    struct Tab: Decodable {
        let url: String?
        let focused: Bool?
    }

    let key: String
    let root: Int
    let ports: [Int]
    let kinds: [String]
    let cwd: [String]
    let footprint: Double
    let peak: Double
    let host: String
    let command: String?
    let terminal: String?
    let clients: [Client]
    let tabs: [Tab]
    let attended: Bool
    let agentWorking: Bool
    let recyclable: Bool
    let quiet: Int
    let protected: Bool
    let background: Bool?
    let servers: Int?
    let launchCwd: String?

    var id: String { key }
    var isStack: Bool { (servers ?? 1) > 1 }

    var title: String {
        isStack ? "Dev stack" : URL(fileURLWithPath: cwd.first ?? "").lastPathComponent
    }

    /// The worktree it runs in: `…/portivo-landing-spacing/apps/landing-page` → `portivo-landing-spacing`.
    var place: String {
        let path = isStack ? (launchCwd ?? cwd.first ?? "") : (cwd.first ?? "")
        let parts = path.split(separator: "/").map(String.init)
        if let apps = parts.lastIndex(where: { $0 == "apps" || $0 == "packages" }), apps > 0 {
            return parts[apps - 1]
        }
        return isStack ? (parts.last ?? path) : (parts.dropLast().last ?? path)
    }

    var portLabel: String {
        guard let first = ports.first else { return "pid \(root)" }
        return ports.count > 1 ? ":\(first) +\(ports.count - 1)" : ":\(first)"
    }

    enum Viewers { case you, agents, headless, nobody }

    var viewers: Viewers {
        if attended { return .you }
        if !tabs.isEmpty || clients.contains(where: { $0.kind == "orca" }) { return .agents }
        if clients.contains(where: { $0.kind == "headless" }) { return .headless }
        return clients.isEmpty ? .nobody : .agents
    }
}

/// Numbers behind a decision; the script's own sentence stays in its Polish log.
struct GuardReason: Decodable {
    let keep: Int?
    let minutes: Int?
    let size: Double?
    let limit: Double?
    let level: Int?
    let total: Double?
    let budget: Double?
    let restarts: Int?
}

struct GuardPlan: Decodable {
    let unit: String
    let action: String
    let code: String?
    let data: GuardReason?
}

struct GuardEvent: Decodable, Identifiable {
    let at: Double
    let action: String
    let label: String
    let size: Double?
    let code: String?
    let data: GuardReason?
    let ports: [Int]?
    let ok: Bool

    var id: Double { at }
}
