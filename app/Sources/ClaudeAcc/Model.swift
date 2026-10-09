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
    /// The limit pause: no account has headroom, so sessions wind down to a checkpoint.
    /// Missing from older scripts, which decodes as no pause.
    let pause: Pause?
    /// Whether the limit pause is on (`claude-acc pause on|off`). Missing from scripts
    /// before 1.10, which had no switch, so the panel hides it.
    let limitPause: Bool?
    /// Whether accounts below the switch threshold get used up when none has headroom
    /// (`claude-acc drain on|off`). Missing from scripts before 1.12.
    let drain: Bool?
    /// The API credits pool (credits.py); nil with no account registered and in older scripts.
    let credits: CreditsSummary?
    let accounts: [Account]

    var active: Account? { accounts.first { $0.active } }
    var others: [Account] { accounts.filter { !$0.active } }
    /// The account auto-switch moves to next.
    var next: Account? {
        others.compactMap { account in account.queue.map { (account, $0) } }.min { $0.1 < $1.1 }?.0
    }
    var anyNeedsLogin: Bool { accounts.contains { $0.status == .needsLogin } }
}

/// Monthly API credits of the Max plans, pooled across their Console organizations. The numbers
/// are the Console reading (`credits balance`) minus spend callers reported since, so they are
/// an estimate between readings; `checkedAt` is the oldest reading behind the total.
struct CreditsSummary: Decodable {
    let totalRemainingUsd: Double
    let grantedUsd: Double
    let linked: Int
    let pending: Int
    let error: Int
    /// Remaining per scope: "own", and one per client whose plan pays only for that client's work.
    let byScope: [String: Double]
    /// The soonest cycle end that still has credit on it, and how much expires then.
    let nextExpiryAt: Double?
    let nextExpiryUsd: Double?
    let checkedAt: Double?

    var spentFraction: Double { grantedUsd > 0 ? max(0, min(1, 1 - totalRemainingUsd / grantedUsd)) : 0 }
}

/// `pause.json` as the script writes it; the hooks in Claude Code sessions read the same file.
struct Pause: Decodable {
    let since: Double
    let account: String
    /// When an account has headroom again, the earliest of their resets; nil when the API gave none.
    let resumeAt: Double?
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

// MARK: - Updates (`updates-state.json`, written by updates.py)

struct UpdatesState: Decodable {
    struct Package: Decodable, Hashable {
        let name: String
        let from: String?
        let to: String?
        let error: String?
        /// The installer wanted an admin password, which a background run can't type.
        let admin: Bool?
        /// The command that retries it by hand, in Terminal.
        let retry: String?
        /// The Python a pip package lives in, like "3.13" or "3.14 Homebrew": numpy can be in two.
        let python: String?
        /// Why a held package stayed: pin, deps, edited, confirm, install, packages.
        let why: String?
        /// A downloaded, signature-checked installer for the user to open (a newer Python).
        let installer: String?

        var label: String { python.map { "\(name) (Python \($0))" } ?? name }
    }

    /// One package manager in the last run: Homebrew, npm, Go, Python or Claude Code.
    struct Step: Decodable, Identifiable {
        let name: String
        let label: String
        let ok: Bool
        let updated: [Package]
        let failed: [Package]
        let held: [Package]?
        let error: String?

        var id: String { name }
    }

    struct Run: Decodable {
        let at: Double
        let ok: Bool
        let updated: Int
        let failed: Int
    }

    let lastRun: Run?
    let lastSuccess: Double?
    /// The 4:30 launchd run that will be due next.
    let nextRun: Double?
    /// A run in progress; a crashed script can leave the mark, so only a fresh one counts.
    let runningSince: Double?
    let steps: [Step]?
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

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            // a setting may record text ("medium" -> "large"): shown without numbers
            before = try? c.decodeIfPresent(Double.self, forKey: .before)
            after = try? c.decodeIfPresent(Double.self, forKey: .after)
            unit = try? c.decodeIfPresent(String.self, forKey: .unit)
        }

        private enum CodingKeys: String, CodingKey { case before, after, unit }
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
        var opensSpotlight = false
        /// System Settings pane to open instead of a command to copy.
        var pane: String?
    }

    let on: Bool
    let since: Double?
    let applied: [String]
    /// Root tweaks perf-root.sh applied: shown with Ultra, undone only by perf-root.sh.
    let rootApplied: [String]
    let pendingRoot: [String]
    let pendingManual: [String]
    let results: [String: Result]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        on = try c.decodeIfPresent(Bool.self, forKey: .on) ?? false
        since = try c.decodeIfPresent(Double.self, forKey: .since)
        applied = try c.decodeIfPresent([String].self, forKey: .applied) ?? []
        rootApplied = try c.decodeIfPresent([String].self, forKey: .rootApplied) ?? []
        pendingRoot = try c.decodeIfPresent([String].self, forKey: .pendingRoot) ?? []
        pendingManual = try c.decodeIfPresent([String].self, forKey: .pendingManual) ?? []
        results = try c.decodeIfPresent([String: Result].self, forKey: .results) ?? [:]
    }

    private enum CodingKeys: String, CodingKey {
        case on, since, applied, rootApplied, pendingRoot, pendingManual, results
    }

    /// perf.py's ULTRA list, in the order it applies them.
    static let order = [
        "bg-helpers", "claude-hooks-async", "claude-hooks-native", "node-compile-cache", "devguard-budget",
        "devguard-max-server", "git-speed", "fast-npx-hooks", "claude-limits", "workflow-size", "claude-ui",
    ]

    static let catalog: [String: Tweak] = [
        "bg-helpers": Tweak(
            title: "Background helpers", detail: "Helpers no agent waits on move to the efficiency cores",
            unit: "% of a P-core"),
        "claude-hooks-async": Tweak(
            title: "Async hooks", detail: "Memory and Orca hooks stop holding up every tool call", unit: "ms per tool call"),
        "claude-hooks-native": Tweak(
            title: "Native Bash hooks", detail: "The guard and rtk check each Bash command in ms, without Python or a shell",
            unit: "ms per Bash command"),
        "node-compile-cache": Tweak(
            title: "Node compile cache", detail: "What sessions spawn (tsc, eslint, MCP servers) starts warm",
            unit: "ms to load TypeScript"),
        "devguard-budget": Tweak(
            title: "Dev server budget", detail: "The guard frees memory sooner", unit: "% of RAM", isSetting: true),
        "docker-vm": Tweak(
            title: "Docker VM", detail: "The VM gives back what containers don't use", unit: "GB", isSetting: true),
        "git-speed": Tweak(
            title: "Git", detail: "fsmonitor and background maintenance in the repos you list, parallel checkout",
            unit: "ms git status"),
        "fast-npx-hooks": Tweak(
            title: "Fast format hooks", detail: "Formatting hooks find eslint and prettier in milliseconds, not seconds",
            unit: "ms per formatted edit"),
        "claude-limits": Tweak(
            title: "Claude Code limits", detail: "Bash commands may run an hour, MCP output doubled",
            unit: "commands cut at 10 min a day"),
        "workflow-size": Tweak(
            title: "Large workflows", detail: "Workflows plan for up to 50 agents instead of 10", unit: "",
            isSetting: true),
        "claude-ui": Tweak(
            title: "Calm Claude Code UI", detail: "Fewer spinner and shimmer frames to repaint, no spinner tips", unit: "",
            isSetting: true),
        "iogpu": Tweak(
            title: "More memory for the GPU", detail: "Local models stay on Metal, set again at every boot (root)",
            unit: "MiB for the GPU", isSetting: true),
        "devtools": Tweak(
            title: "Go tests skip Gatekeeper", detail: "Orca is a developer tool, so fresh test binaries start without a check",
            unit: "ms first run of a new binary"),
        "vnodes": Tweak(
            title: "Bigger file cache", detail: "Three times the vnodes, set again at every boot (root)",
            unit: "s to rescan node_modules"),
        "spotlight": Tweak(
            title: "Spotlight: apps only", detail: "Home folders and system data left out of the index (root)",
            unit: "files besides apps", isSetting: true),
        "shaper": Tweak(
            title: "Upload shaper", detail: "Uploads queue on the Mac instead of in the router (root)", unit: "ms queue"),
        "devguard-max-server": Tweak(
            title: "Dev server size limit", detail: "The guard restarts a bloated server sooner", unit: "GB per server",
            isSetting: true),
    ]

    /// A tweak this app doesn't know yet: its name in words, numbers shown plainly.
    static func tweak(_ name: String) -> Tweak {
        catalog[name] ?? Tweak(
            title: name.replacingOccurrences(of: "-", with: " ").capitalized, detail: "", unit: "", isSetting: true)
    }

    static let steps: [String: Step] = [
        "vnodes": Step(
            title: "Bigger file cache", detail: "The vnode cache is full, so every scan of node_modules starts cold. Needs root.",
            command: "claude-acc perf-root vnodes trial"),
        "shaper": Step(
            title: "Upload shaper", detail: "Uploads queue in the router. Keeping the queue on the Mac needs root.",
            command: "claude-acc perf-root trial"),
        "spotlight-privacy": Step(
            title: "Hide caches from Spotlight",
            detail: "Spotlight indexes package caches (~/Library/pnpm, ~/go) for nothing. Add them under Search Privacy.",
            command: nil, opensSpotlight: true),
        "devtools": Step(
            title: "Make Orca a developer tool",
            detail: "Every new Go test binary waits ~0.2 s for Gatekeeper. Click +, pick Orca, confirm with Touch ID.",
            command: nil,
            pane: "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_DevTools"),
        "devtools-restart": Step(
            title: "Restart Orca once",
            detail: "Developer Tools applies to Orca started after the change. A restart closes the sessions in its terminals.",
            command: nil),
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

// MARK: - Hotspot turbo (`hotspot-state.json`, written by the root hotspot daemon)

struct HotspotState: Decodable, Equatable {
    let at: Double
    let enabled: Bool
    let active: Bool
    let iface: String?
    let via: String?
    let rateKbps: Int?
    let txKbps: Int?
    let delayP90Ms: Double?
    /// False while no probe comes back: the daemon lifts the limit rather than shape blind.
    let shaping: Bool?
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

// MARK: - Mail gateway (`mail/panel.json`, written by mail.py on every call and check)

struct MailPanel: Decodable {
    struct Health: Decodable {
        let ok: Bool
        let detail: String?
        /// A short English reason for the panel; `detail` keeps the script's full message.
        let reason: String?
        let checkedAt: Double?
    }

    struct Mailbox: Decodable, Identifiable {
        let mailbox: String
        /// gmail or imap
        let provider: String
        /// read, modify or draft
        let access: String
        /// off, ask or auto
        let send: String
        let health: Health?
        let lastUsed: Double?
        let callsToday: Int
        let errorsToday: Int

        var id: String { mailbox }
    }

    struct Call: Decodable, Identifiable {
        let at: Double
        let client: String?
        let tool: String
        let mailbox: String?
        let ok: Bool
        let detail: String?

        var id: String { "\(at)-\(tool)-\(mailbox ?? "")" }
    }

    let configured: Bool
    let error: String?
    let identity: Health?
    let checkedAt: Double?
    let mcpRegistered: Bool
    let mailboxes: [Mailbox]
    let recent: [Call]
}

// MARK: - Browser gateway (`browser/panel.json`, written by browser.py's daemon and `status`)

struct BrowserPanel: Decodable {
    struct Browser: Decodable, Identifiable {
        let name: String
        let title: String
        let installed: Bool
        /// connected, connecting (the browser asks for Allow), ready, closed, disabled, missing or error
        let state: String
        let error: String?
        let since: Double?
        let tabs: Int
        /// The page with the remote debugging checkbox (chrome://inspect/#remote-debugging).
        let inspect: String

        var id: String { name }
    }

    struct Tab: Decodable, Identifiable {
        let tab: String
        let browser: String
        /// hidden, background or user
        let mode: String
        let handed: Bool
        let title: String
        let url: String
        let client: String?

        var id: String { tab }
    }

    struct Call: Decodable, Identifiable {
        let at: Double
        let op: String
        let tab: String?
        let browser: String?
        let url: String
        let ok: Bool

        var id: String { "\(at)-\(op)-\(tab ?? "")" }
    }

    let error: String?
    let installed: Bool
    let mcpRegistered: Bool
    let hub: Bool
    let `default`: String?
    let tabsMode: String
    let idleMinutes: Int
    let browsers: [Browser]
    let tabs: [Tab]
    let clients: Int
    let recent: [Call]
}

// MARK: - Desktop gateway (`desktop/panel.json`, written by desktop.py's status and tool calls)

struct DesktopPanel: Decodable {
    struct Display: Decodable, Identifiable {
        let id: Int
        let w: Double
        let h: Double
        let main: Bool
    }

    struct Front: Decodable {
        let bundle: String?
        let window: String?
    }

    struct Call: Decodable, Identifiable {
        let at: Double
        let member: String
        let ok: Bool
        let app: String?

        var id: String { "\(at)-\(member)" }
    }

    /// Permissions as an agent gets them: TCC grants belong to the app the helper runs under (Orca for
    /// agents in its terminals), so the menu bar app's own check would show them off.
    struct Agent: Decodable {
        let ax: Bool
        let screen: Bool
        let host: String?
        let at: Double
    }

    let error: String?
    let installed: Bool
    let mcpRegistered: Bool
    let helper: String
    let helperPresent: Bool
    let ax: Bool
    let screen: Bool
    let post: Bool
    let mode: String
    let displays: [Display]
    let frontmost: Front?
    let recent: [Call]
    let probeError: String?
    let agent: Agent?
    // klucze snake_case (mcp_registered, helper_present, probe_error) mapuje .convertFromSnakeCase dekodera
}
