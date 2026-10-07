import SwiftUI

/// The agents' mail gateway (mail.py): which mailboxes the MCP server opens, whether each one
/// answers and what it allows, and the last call an agent made. Compact on purpose: it sits under
/// the builds, and the full history is in mail/audit.jsonl.
struct MailCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Mail", symbol: "envelope") {
            VStack(alignment: .leading, spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(failing > 0 || panel?.configured == false ? .orange : .primary)
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                if let mailboxes = panel?.mailboxes, !mailboxes.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        ForEach(mailboxes) { MailboxRow(mailbox: $0) }
                    }
                }
            }
        } accessory: {
            if store.mailChecking {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Checking…").font(.caption).foregroundStyle(.secondary)
                }
            } else if panel?.configured == true {
                Button("Check", systemImage: "checkmark.circle") { Task { await store.checkMail() } }
                    .panelButton()
                    .controlSize(.small)
                    .help("Sign in to every mailbox once and show whether it answers")
            }
        }
    }

    private var panel: MailPanel? { store.mail }

    private var failing: Int { panel?.mailboxes.filter { $0.health?.ok == false }.count ?? 0 }

    private var headline: String {
        guard let panel, panel.configured else { return "Not set up" }
        if failing > 0 { return failing == 1 ? "1 mailbox doesn't answer" : "\(failing) mailboxes don't answer" }
        let calls = panel.mailboxes.reduce(0) { $0 + $1.callsToday }
        let count = panel.mailboxes.count == 1 ? "1 mailbox" : "\(panel.mailboxes.count) mailboxes"
        return calls == 0 ? count : "\(count) · \(calls == 1 ? "1 call" : "\(calls) calls") today"
    }

    private var subline: String {
        guard let panel, panel.configured else {
            return "claude-acc mail add <address> gmail|imap, then claude-acc mail install-mcp"
        }
        var parts: [String] = []
        if let identity = panel.identity, !identity.ok {
            parts.append("Google: \(identity.reason ?? "sign-in failed")")
        }
        if !panel.mcpRegistered { parts.append("MCP off: claude-acc mail install-mcp") }
        if let call = panel.recent.first {
            parts.append("last: \(callLabel(call)) \(Format.ago(call.at, now: now))")
        } else if let checked = panel.checkedAt {
            parts.append("checked \(Format.ago(checked, now: now))")
        }
        return parts.isEmpty ? "MCP on for Claude Code" : parts.joined(separator: " · ")
    }

    private func callLabel(_ call: MailPanel.Call) -> String {
        let tool = call.tool.replacingOccurrences(of: "mail_", with: "")
        let box = call.mailbox?.split(separator: "@").first.map(String.init) ?? ""
        return [tool, box].filter { !$0.isEmpty }.joined(separator: " ") + (call.ok ? "" : " (failed)")
    }
}

/// One mailbox on one line: whether it answers, the address, the provider and the send mode;
/// what it answered (or why not) is in the tooltip, an error also under the line.
private struct MailboxRow: View {
    let mailbox: MailPanel.Mailbox
    @Environment(\.now) private var now

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 7) {
                StatusDot(color: color)
                    .frame(width: 14)
                Text(mailbox.mailbox)
                    .font(.callout)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .layoutPriority(1)
                Spacer(minLength: 6)
                Text(level)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .fixedSize()
            }
            if let health = mailbox.health, !health.ok {
                Text(health.reason ?? health.detail ?? "doesn't answer")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(1)
                    .padding(.leading, 21)
            }
        }
        .help(tooltip)
    }

    private var color: Color {
        switch mailbox.health?.ok {
        case true: .green
        case false: .red
        default: .gray
        }
    }

    private var level: String {
        let provider = mailbox.provider == "gmail" ? "Gmail" : "IMAP"
        let send = switch mailbox.send {
        case "auto": "sends"
        case "ask": "asks to send"
        default: "drafts"
        }
        return "\(provider) · \(send)"
    }

    private var tooltip: String {
        var parts = ["\(mailbox.access) access"]
        if let detail = mailbox.health?.detail { parts.append(detail) }
        if mailbox.callsToday > 0 { parts.append("\(mailbox.callsToday) calls today") }
        if let used = mailbox.lastUsed { parts.append("used \(Format.ago(used, now: now))") }
        return parts.joined(separator: " · ")
    }
}
