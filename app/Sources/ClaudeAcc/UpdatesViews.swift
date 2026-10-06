import SwiftUI

/// Homebrew, npm and Go kept current by updates.py: when it last worked, what each package
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
                    .help("Upgrade Homebrew, npm and Go packages now")
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

/// One package manager: what the last run did, failures spelled out under it.
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
        }
    }

    private var symbol: String {
        if step.error != nil { return "xmark.octagon.fill" }
        return step.failed.isEmpty ? "checkmark.circle.fill" : "exclamationmark.triangle.fill"
    }

    private var tint: Color {
        if step.error != nil { return .red }
        return step.failed.isEmpty ? .green : .orange
    }

    private var detail: String {
        if step.error != nil { return "didn't run" }
        var parts = [step.updated.isEmpty ? "up to date" : "\(step.updated.count) updated"]
        if !step.failed.isEmpty { parts.append("\(step.failed.count) failed") }
        // brew and npm hold what you pinned; pip holds what another package needs lower
        if let held = step.held, !held.isEmpty { parts.append("\(held.count) \(step.name == "pip" ? "held back" : "pinned")") }
        return parts.joined(separator: " · ")
    }

    private var problems: [String] {
        if let error = step.error { return [error] }
        return step.failed.prefix(3).map { failed in
            if failed.admin == true, let retry = failed.retry {
                return "\(failed.name) needs your admin password. In Terminal: \(retry)"
            }
            return "\(failed.label): \(failed.error ?? "failed")"
        }
    }

    private var tooltip: String {
        var lines = step.updated.map { "\($0.label) \($0.from ?? "?") → \($0.to ?? "?")" }
        lines += (step.held ?? []).map { "\($0.label) kept at \($0.from ?? "?") (\($0.to ?? "?") is out)" }
        return lines.isEmpty ? "Nothing to update last time" : lines.joined(separator: "\n")
    }
}
