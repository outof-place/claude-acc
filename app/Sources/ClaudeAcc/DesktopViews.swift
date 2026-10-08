import SwiftUI

/// The agents' desktop gateway (desktop.py): whether the native helper has the two permissions it needs
/// (Accessibility, Screen Recording), the active displays, the mode, and the last thing an agent did.
/// When a permission is off, Open Settings takes the user to the right pane to tick the helper binary.
struct DesktopCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Desktop", symbol: "macwindow.on.rectangle") {
            VStack(alignment: .leading, spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(ready ? AnyShapeStyle(.primary) : AnyShapeStyle(.orange))
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if panel != nil {
                    VStack(alignment: .leading, spacing: 5) {
                        PermissionRow(label: "Accessibility", granted: panel?.ax ?? false)
                        PermissionRow(label: "Screen Recording", granted: panel?.screen ?? false)
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

    private var headline: String {
        guard let panel else { return "Not set up" }
        if !panel.helperPresent { return "Helper missing" }
        if ready { return "Ready" }
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
        } else if !panel.helperPresent {
            parts.append("build app/build.sh, then setup.sh")
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

/// One permission on one line: a dot and whether the helper has it.
private struct PermissionRow: View {
    let label: String
    let granted: Bool

    var body: some View {
        HStack(spacing: 7) {
            StatusDot(color: granted ? .green : .orange)
                .frame(width: 14)
            Text(label)
                .font(.callout)
                .lineLimit(1)
            Spacer(minLength: 6)
            Text(granted ? "granted" : "off")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize()
        }
    }
}
