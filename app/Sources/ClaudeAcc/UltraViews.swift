import AppKit
import SwiftUI

/// One switch that tunes the Mac for agent work. perf.py applies the tweaks, measures each one
/// before and after, and on Off puts back exactly what was there; this card shows the numbers.
struct UltraCard: View {
    let store: Store
    @Environment(\.now) private var now
    @Environment(\.animating) private var animating

    var body: some View {
        Card("Ultra", symbol: "bolt.fill") {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(ultra?.on == true ? Format.violet : .secondary)
                        .contentTransition(.interpolate)
                    Text(subline)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                CardScroll {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(rows, id: \.self) { name in
                            UltraRow(
                                name: name, result: ultra?.on == true ? ultra?.results[name] : nil,
                                active: ultra?.on == true, waiting: waiting(name))
                        }
                        if !needs.isEmpty {
                            Divider()
                            Text("Needs you")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            ForEach(needs, id: \.self) { name in
                                UltraStep(name: name) { Task { await store.openSpotlightSettings() } }
                            }
                        }
                    }
                }
            }
        } accessory: {
            HStack(spacing: 8) {
                if store.ultraBusy, animating {
                    ProgressView().controlSize(.mini)
                }
                Toggle("Ultra", isOn: Binding(
                    get: { store.ultraPick ?? ultra?.on ?? false },
                    set: { on in Task { await store.setUltra(on) } }))
                    .toggleStyle(PillToggleStyle(tint: Format.violet))
                    .labelsHidden()
                    .disabled(store.ultraBusy)
            }
        }
        .animation(.smooth(duration: 0.3), value: ultra?.on)
    }

    private var ultra: Ultra? { store.ultra }

    private var headline: String {
        if store.ultraBusy { return store.ultraPick == true ? "Tuning…" : "Undoing…" }
        return ultra?.on == true ? "Tuned for agents" : "Off"
    }

    private var subline: String {
        guard let ultra, ultra.on else {
            return "Turn it on to tune the Mac for agents. Every change is measured, and Off puts back exactly what was there."
        }
        let since = ultra.since.map { " since \(Format.moment($0, now: now))" } ?? ""
        return "\(ultra.applied.count) changes\(since)"
    }

    /// On: what Ultra applied; off: what it would do.
    private var rows: [String] {
        if let ultra, ultra.on { return ultra.applied }
        return Ultra.order
    }

    private var needs: [String] {
        guard let ultra, ultra.on else { return [] }
        return ultra.pendingManual + ultra.pendingRoot
    }

    private func waiting(_ name: String) -> String? {
        guard let ultra, ultra.on, name == "docker-vm" else { return nil }
        if ultra.pendingManual.contains("docker-quit") { return "Written when Docker is closed" }
        if ultra.pendingManual.contains("docker-restart") { return "Applies after Docker restarts" }
        return nil
    }
}

/// A tweak and its numbers: before → after, with a bar for measured gains.
private struct UltraRow: View {
    let name: String
    let result: Ultra.Result?
    let active: Bool
    let waiting: String?

    private var tweak: Ultra.Tweak { Ultra.tweak(name) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(tweak.title)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(active ? .primary : .secondary)
                    Text(waiting ?? tweak.detail)
                        .font(.caption)
                        .foregroundStyle(waiting == nil ? AnyShapeStyle(.secondary) : AnyShapeStyle(.orange))
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 6)
                if active { value }
            }
            if active, let result, let after = result.after, let before = result.before, before > 0, !tweak.isSetting {
                GainBar(before: before, after: after)
            }
        }
    }

    @ViewBuilder private var value: some View {
        if let result, let before = result.before {
            VStack(alignment: .trailing, spacing: 1) {
                HStack(spacing: 4) {
                    Text(Ultra.number(before)).foregroundStyle(.secondary)
                    Image(systemName: "arrow.right").font(.caption2).foregroundStyle(.tertiary)
                    Text(result.after.map(Ultra.number) ?? "…")
                        .foregroundStyle(gain == nil ? AnyShapeStyle(.primary) : AnyShapeStyle(Format.violet))
                }
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                Text(result.after == nil ? "measuring" : tweak.unit)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .fixedSize()
        } else {
            Chip("On", tint: Format.violet)
        }
    }

    /// Lower is better for every measured tweak: the share of the old value that's gone.
    private var gain: Double? {
        guard !tweak.isSetting, let before = result?.before, let after = result?.after, before > 0, after < before
        else { return nil }
        return 1 - after / before
    }
}

/// Before as a quiet track, after as the violet part of it, and the drop in percent.
private struct GainBar: View {
    let before: Double
    let after: Double

    var body: some View {
        HStack(spacing: 8) {
            UsageBar(fraction: max(after / before, 0.015), tint: Format.violet, height: 5)
            // floor, so 99.7% reads as 99%, never as a whole 100%
            Text(after < before ? "−\(Int(((1 - after / before) * 100).rounded(.down)))%" : "no gain")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(after < before ? AnyShapeStyle(.green) : AnyShapeStyle(.secondary))
                .monospacedDigit()
                .frame(width: 38, alignment: .trailing)
        }
    }
}

/// Something Ultra can't do alone: a root command to copy, or a click in Docker.
private struct UltraStep: View {
    let name: String
    let openSpotlight: () -> Void

    var body: some View {
        let step = Ultra.steps[name] ?? Ultra.Step(
            title: name.replacingOccurrences(of: "-", with: " ").capitalized, detail: "", command: nil)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(step.title).font(.callout.weight(.medium))
                Text(step.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if let command = step.command {
                Button("Copy", systemImage: "doc.on.doc") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(command, forType: .string)
                }
                .panelButton()
                .controlSize(.small)
                .help(command)
            } else if step.opensSpotlight {
                Button("Open", systemImage: "gearshape", action: openSpotlight)
                    .panelButton()
                    .controlSize(.small)
                    .help("System Settings › Spotlight")
            }
        }
    }
}
