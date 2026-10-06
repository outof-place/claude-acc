import AppKit
import SwiftUI

/// Homebrew, npm, Go, Python and Claude Code kept current by updates.py: when it last worked, what each package
/// manager did in that run, and a button to run it now.
struct UpdatesCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Updates", symbol: "arrow.down.circle") {
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(lastRun?.ok == false ? .orange : .primary)
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let steps = store.updates?.steps, !steps.isEmpty {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(steps) { UpdateStepRow(step: $0) }
                    }
                }
            }
        } accessory: {
            if running {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Updating…").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Button("Update", systemImage: "arrow.down.circle") { Task { await store.runUpdates() } }
                    .panelButton()
                    .controlSize(.small)
                    .help("Update Homebrew, npm, Go, Python and Claude Code now")
            }
        }
    }

    private var lastRun: UpdatesState.Run? { store.updates?.lastRun }

    private var running: Bool {
        if store.updating { return true }
        guard let since = store.updates?.runningSince else { return false }
        return now.timeIntervalSince1970 - since < 2 * 3600
    }

    private var headline: String {
        guard let lastRun else { return "Not run yet" }
        if !lastRun.ok { return lastRun.failed == 1 ? "1 update failed" : "\(lastRun.failed) updates failed" }
        return "Updated \(Format.ago(store.updates?.lastSuccess, now: now))"
    }

    private var subline: String {
        guard let lastRun else { return "Click Update, or wait for the nightly run" }
        let next = next
        if !lastRun.ok {
            let success = store.updates?.lastSuccess.map { "Last success \(Format.ago($0, now: now))" } ?? "No clean run yet"
            return "\(success) · retry \(next)"
        }
        let count = lastRun.updated == 0 ? "Nothing new" : lastRun.updated == 1 ? "1 package" : "\(lastRun.updated) packages"
        return "\(count) · next \(next)"
    }

    private var next: String {
        guard let at = store.updates?.nextRun else { return "tonight" }
        return at > now.timeIntervalSince1970 ? Format.stamp(at, now: now) : "at the next 04:30"
    }
}

/// One package manager: what the last run did, failures and what needs you spelled out under it.
private struct UpdateStepRow: View {
    let step: UpdatesState.Step

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 7) {
                Image(systemName: symbol)
                    .imageScale(.small)
                    .foregroundStyle(tint)
                    .frame(width: 14)
                Text(step.label).font(.callout)
                Spacer(minLength: 8)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            .help(tooltip)
            ForEach(problems, id: \.self) { line in
                Text(line)
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .padding(.leading, 21)
                    .help(line)
            }
            ForEach(installers, id: \.self) { offer in
                HStack(spacing: 6) {
                    Text("\(offer.name) \(offer.to ?? "") is out")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    Button("Install") {
                        if let path = offer.installer { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
                    }
                    .panelButton()
                    .controlSize(.mini)
                    .help("Opens the python.org installer, checked for its signature. It asks for your password.")
                }
                .padding(.leading, 21)
            }
        }
    }

    private var held: [UpdatesState.Package] { step.held ?? [] }

    private var installers: [UpdatesState.Package] { held.filter { $0.why == "install" && $0.installer != nil } }

    private var symbol: String {
        if step.error != nil { return "xmark.octagon.fill" }
        return step.failed.isEmpty ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
    }

    private var tint: Color {
        if step.error != nil { return .red }
        return step.failed.isEmpty ? .green : .orange
    }

    private var detail: String {
        if step.error != nil { return "didn't update" }
        var parts = [step.updated.isEmpty ? "up to date" : "\(step.updated.count) updated"]
        if !step.failed.isEmpty { parts.append("\(step.failed.count) failed") }
        if !held.isEmpty { parts.append("\(held.count) held") }
        return parts.joined(separator: " · ")
    }

    /// Failures, then what waits for a person: a plugin command to confirm.
    private var problems: [String] {
        if let error = step.error { return [error] }
        let failed = step.failed.prefix(3).map { failed in
            if failed.admin == true, let retry = failed.retry {
                return "\(failed.name) needs your admin password. In Terminal: \(retry)"
            }
            let retry = failed.retry.map { ". In Terminal: \($0)" } ?? ""
            return "\(failed.name): \(failed.error ?? "failed")\(retry)"
        }
        let confirm = held.filter { $0.why == "confirm" }.map { held in
            "\(held.name) wants to run a command. Review it in Terminal: \(held.retry ?? "claude plugin update")"
        }
        return failed + confirm
    }

    private var tooltip: String {
        var lines = step.updated.map { "\($0.name) \($0.from ?? "?") → \($0.to ?? "?")" }
        lines += held.map { held in
            let newer = held.to.map { ", \($0) is out" } ?? ""
            return switch held.why {
            case "pin": "\(held.name) pinned at \(held.from ?? "?")\(newer)"
            case "deps": "\(held.name) kept at \(held.from ?? "?") by other packages\(newer)"
            case "edited": "\(held.name) edited by hand, not overwritten"
            case "confirm": "\(held.name) waits for you to confirm its command"
            case "install": "\(held.name) \(held.to ?? "") is ready to install"
            default: "\(held.name) kept at \(held.from ?? "?")\(newer)"
            }
        }
        return lines.isEmpty ? "Nothing to update last time" : lines.joined(separator: "\n")
    }
}
