import SwiftUI

/// The agents' desktop gateway (desktop.py): whether the native helper has the two permissions it needs
/// (Accessibility, Screen Recording), the active displays, the mode, and the last thing an agent did.
/// When a permission is off, Open Settings takes the user to the right pane to tick the helper binary.
/// The permissions shown are the ones an agent gets (checked from its session, e.g. in Orca): macOS ties
/// them to the app the helper runs under, so this app checking for itself would see them off.
struct DesktopCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Desktop", symbol: "macwindow.on.rectangle") {
            VStack(alignment: .leading, spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(ready ? AnyShapeStyle(.primary)
                                         : unchecked ? AnyShapeStyle(.secondary) : AnyShapeStyle(.orange))
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if panel != nil {
                    VStack(alignment: .leading, spacing: 5) {
                        PermissionRow(label: "Accessibility", granted: panel?.ax ?? false, host: host)
                        PermissionRow(label: "Screen Recording", granted: panel?.screen ?? false, host: host)
                    }
                }
            }
        } accessory: {
            if let panel, !panel.ax || !panel.screen || !panel.helperPresent {
                Button("Open Settings", systemImage: "gearshape") { Task { await store.openDesktopPermissions() } }
                    .panelButton()
                    .controlSize(.small)
                    .disabled(!panel.helperPresent)
                    .help(panel.helperPresent ? "Open the Privacy pane to tick the helper binary"
                                              : "Build and install the helper first")
            }
        }
    }

    private var panel: DesktopPanel? { store.desktop }

    private var ready: Bool { (panel?.ax ?? false) && (panel?.screen ?? false) && (panel?.helperPresent ?? false) }

    /// No agent has used the gateway yet, so nobody has seen the grants where they apply.
    private var unchecked: Bool { panel != nil && panel?.agent == nil && !ready }

    private var host: String? { panel?.agent.map { $0.host ?? "the agent's app" } }

    private var headline: String {
        guard let panel else { return "Not set up" }
        if !panel.helperPresent { return "Helper missing" }
        if ready { return "Ready" }
        if panel.agent == nil { return "Not checked by an agent yet" }
        if !panel.ax && !panel.screen { return "Needs permissions" }
        return !panel.ax ? "Needs Accessibility" : "Needs Screen Recording"
    }

    private var subline: String {
        guard let panel else { return "claude-acc desktop install" }
        var parts: [String] = []
        if !panel.mcpRegistered { parts.append("MCP off: claude-acc desktop install") }
        else if let call = panel.recent.first {
            parts.append("last: \(call.member)\(call.ok ? "" : " (failed)") \(app(call.app)) \(Format.ago(call.at, now: now))")
        } else if ready {
            let screens = panel.displays.map { "\(Int($0.w))x\(Int($0.h))" }.joined(separator: ", ")
            parts.append("mode \(panel.mode) · \(screens.isEmpty ? "active display" : screens)")
            if let agent = panel.agent { parts.append("checked \(Format.ago(agent.at, now: now))") }
        } else if !panel.helperPresent {
            parts.append("build app/build.sh, then setup.sh")
        } else if panel.agent == nil {
            parts.append("Grants belong to the app that runs the agent (Orca, Terminal); its first desktop call checks them")
        } else {
            parts.append("Tick the helper binary in System Settings")
        }
        return parts.joined(separator: " · ")
    }

    private func app(_ bundle: String?) -> String {
        guard let bundle, let last = bundle.split(separator: ".").last else { return "" }
        return String(last)
    }
}

/// One permission on one line: a dot and whether the helper has it in the app agents run in.
private struct PermissionRow: View {
    let label: String
    let granted: Bool
    let host: String?

    var body: some View {
        HStack(spacing: 7) {
            StatusDot(color: granted ? .green : host == nil ? .secondary : .orange)
                .frame(width: 14)
            Text(label)
                .font(.callout)
                .lineLimit(1)
            Spacer(minLength: 6)
            Text((granted ? "granted" : host == nil ? "not checked" : "off") + (host.map { " in \($0)" } ?? ""))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize()
        }
    }
}
