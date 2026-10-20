import SwiftUI

/// The agents' browser gateway (browser.py): whether Chrome and Brave let agents in, which one is
/// connected and how many background tabs agents have open, and the last thing an agent did.
/// Disconnect closes the agents' tabs and the connection, so the automation bar goes away.
/// Blocked: macOS won't let the daemon read the browser's profile, where the browser writes its
/// debugging port; the fix is Full Disk Access for the app the daemon runs under, once.
struct BrowserCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Browser", symbol: "safari") {
            VStack(alignment: .leading, spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(Self.headline(panel))
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(waiting || blocked ? .orange : .primary)
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if let browsers = panel?.browsers.filter(\.installed), !browsers.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(browsers) { browser in
                            BrowserRow(browser: browser, isDefault: browser.name == panel?.default,
                                       accessApp: panel?.accessApp) {
                                Task { await store.enableBrowser(browser) }
                            } grant: {
                                store.openFullDiskAccess(for: panel?.accessApp)
                            }
                        }
                    }
                }
            }
        } accessory: {
            if store.browserBusy {
                ProgressView().controlSize(.mini)
            } else if connected {
                Button("Disconnect", systemImage: "xmark.circle") { Task { await store.disconnectBrowser() } }
                    .panelButton()
                    .controlSize(.small)
                    .help("Close the agents' tabs and the connection; the next agent asks for Allow again")
            }
        }
    }

    private var panel: BrowserPanel? { store.browser }

    private var connected: Bool { panel?.browsers.contains { $0.state == "connected" } ?? false }

    private var waiting: Bool { panel?.browsers.contains { $0.state == "connecting" } ?? false }

    private var blocked: Bool { Self.blocked(panel) }

    /// Blocked, and nothing else working: the one thing to fix is the grant.
    static func blocked(_ panel: BrowserPanel?) -> Bool {
        guard let panel else { return false }
        let states = Set(panel.browsers.filter(\.installed).map(\.state))
        return states.contains("blocked") && states.isDisjoint(with: ["connected", "connecting", "ready"])
    }

    static func headline(_ panel: BrowserPanel?) -> String {
        guard let panel else { return "Not set up" }
        if let asking = panel.browsers.first(where: { $0.state == "connecting" }) {
            return "Click Allow in \(asking.title)"
        }
        let tabs = panel.tabs.count
        if panel.browsers.contains(where: { $0.state == "connected" }) {
            let names = panel.browsers.filter { $0.state == "connected" }.map(\.title).joined(separator: " and ")
            return tabs == 0 ? "\(names) connected" : "\(names) · \(tabs == 1 ? "1 agent tab" : "\(tabs) agent tabs")"
        }
        if panel.browsers.contains(where: { $0.state == "ready" }) { return "Ready" }
        if blocked(panel) { return "Needs Full Disk Access" }
        if panel.browsers.contains(where: { $0.state == "closed" }) { return "Browser closed" }
        return "Remote debugging off"
    }

    private var subline: String {
        guard let panel else { return "claude-acc browser install" }
        var parts: [String] = []
        if !panel.mcpRegistered { parts.append("MCP off: claude-acc browser install") }
        if blocked {
            parts.append("macOS keeps the browser profile closed to \(panel.accessApp?.name ?? "the gateway"): turn it on in Full Disk Access, once")
        } else if let call = panel.recent.first {
            parts.append("last: \(call.op)\(call.ok ? "" : " (failed)") \(host(call.url)) \(Format.ago(call.at, now: now))")
        } else {
            parts.append(panel.tabsMode == "hidden" ? "Agent tabs stay hidden, never take focus" : "Agent tabs open in the background")
        }
        return parts.joined(separator: " · ")
    }

    private func host(_ url: String) -> String {
        URL(string: url)?.host() ?? ""
    }
}

/// One browser on one line: whether agents can get in, and what to do when they can't.
private struct BrowserRow: View {
    let browser: BrowserPanel.Browser
    let isDefault: Bool
    let accessApp: BrowserPanel.AccessApp?
    let enable: () -> Void
    let grant: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 7) {
                StatusDot(color: color)
                    .frame(width: 14)
                Text(browser.title + (isDefault ? " (default)" : ""))
                    .font(.callout)
                    .lineLimit(1)
                    .layoutPriority(1)
                Spacer(minLength: 6)
                if browser.state == "disabled" {
                    Button("Turn on", action: enable)
                        .buttonStyle(.link)
                        .font(.caption)
                        .help("Opens \(browser.inspect) in \(browser.title): tick Allow remote debugging for this browser instance (once)")
                } else if browser.state == "blocked" {
                    Button("Grant Full Disk Access", action: grant)
                        .buttonStyle(.link)
                        .font(.caption)
                        .help("macOS won't let \(accessApp?.name ?? "the gateway") read \(browser.title)'s profile, where it writes its debugging port. Turn on \(accessApp?.name ?? "the app") in Full Disk Access (once)\(accessApp.map { ": \($0.path)" } ?? "")")
                } else {
                    Text(state)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize()
                }
            }
            if let error = browser.error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(1)
                    .padding(.leading, 21)
                    .help(error)
            }
        }
    }

    private var color: Color {
        switch browser.state {
        case "connected": .green
        case "connecting", "blocked": .orange
        case "error": .red
        case "ready": .mint
        default: .gray
        }
    }

    private var state: String {
        switch browser.state {
        case "connected": browser.tabs == 0 ? "connected" : "connected · \(browser.tabs) tabs"
        case "connecting": "asks for Allow"
        case "ready": "ready"
        case "closed": "not running"
        case "error": "refused"
        default: browser.state
        }
    }
}
