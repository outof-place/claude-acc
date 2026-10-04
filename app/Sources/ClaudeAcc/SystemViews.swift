import SwiftUI

// MARK: - Stay Awake

struct AwakeCard: View {
    let awake: Awake
    @Environment(\.now) private var now

    private static let presets: [(label: String, seconds: TimeInterval?)] = [
        ("∞", nil), ("1h", 3600), ("2h", 7200), ("4h", 14_400), ("8h", 28_800),
    ]

    var body: some View {
        Card("Stay Awake", symbol: "cup.and.heat.waves.fill") {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(awake.isOn ? Format.violet : .secondary)
                        .contentTransition(.interpolate)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                GlassEffectContainer(spacing: 6) {
                    HStack(spacing: 6) {
                        ForEach(Self.presets, id: \.label) { preset in
                            Button(preset.label) { awake.stayAwake(for: preset.seconds) }
                                .glassButton(prominent: selected(preset.seconds))
                                .controlSize(.small)
                                .help(preset.seconds == nil ? "Until you turn it off" : "For \(preset.label)")
                        }
                    }
                }
                VStack(spacing: 8) {
                    SettingRow("Keep the display on", symbol: "display", isOn: binding(\.keepDisplayOn))
                    SettingRow("Auto on any hotspot", symbol: "personalhotspot", isOn: binding(\.autoOnHotspot))
                    SettingRow("Keep the hotspot alive", symbol: "antenna.radiowaves.left.and.right", isOn: binding(\.keepHotspotAlive))
                }
                hotspotLine
            }
        } accessory: {
            Toggle("Stay awake", isOn: Binding(
                get: { awake.isOn },
                set: { $0 ? awake.stayAwake(for: nil) : awake.turnOff() }))
                .toggleStyle(PillToggleStyle(tint: Format.violet))
                .labelsHidden()
        }
        .animation(.smooth, value: awake.isOn)
    }

    private var title: String {
        if awake.manualActive { return awake.manualUntil == .distantFuture ? "Awake" : "Awake for a while" }
        return awake.hotspotActive ? "Awake on hotspot" : "Off"
    }

    private var detail: String {
        if awake.manualActive, let until = awake.manualUntil, until != .distantFuture {
            return "\(Format.countdown(until.timeIntervalSince1970, now: now)) left · until \(Format.moment(until.timeIntervalSince1970, now: now))"
        }
        if awake.manualActive { return "Until you turn it off" }
        if awake.hotspotActive { return "Stays on while the Mac uses the hotspot" }
        return awake.autoOnHotspot ? "Turns on by itself on a hotspot" : "Sleeps as usual"
    }

    private func selected(_ seconds: TimeInterval?) -> Bool {
        guard awake.manualActive, let until = awake.manualUntil else { return false }
        guard let seconds else { return until == .distantFuture }
        let left = until.timeIntervalSince(now)
        return until != .distantFuture && left <= seconds && left > seconds - 1800
    }

    @ViewBuilder private var hotspotLine: some View {
        HStack(spacing: 6) {
            StatusDot(color: awake.onHotspot ? (awake.keepAliveFailing ? .orange : .green) : .gray, size: 7)
            if awake.onHotspot {
                let via = awake.hotspotVia.map { " via \($0)" } ?? ""
                let ping = awake.lastKeepAlive.map { " · keep-alive \(Format.ago($0.timeIntervalSince1970, now: now))" } ?? ""
                Text("On a hotspot\(via)\(awake.keepAliveFailing ? " · no internet" : ping)")
            } else {
                Text("Not on a hotspot")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .lineLimit(1)
    }

    private func binding(_ key: ReferenceWritableKeyPath<Awake, Bool>) -> Binding<Bool> {
        Binding(get: { awake[keyPath: key] }, set: { awake[keyPath: key] = $0 })
    }
}

/// Label on the left, a small switch on the right.
struct SettingRow: View {
    let title: String
    let symbol: String
    @Binding var isOn: Bool

    init(_ title: String, symbol: String, isOn: Binding<Bool>) {
        self.title = title
        self.symbol = symbol
        _isOn = isOn
    }

    var body: some View {
        HStack {
            Label(title, systemImage: symbol)
                .font(.callout)
                .labelStyle(SettingLabelStyle())
            Spacer(minLength: 8)
            Toggle(title, isOn: $isOn)
                .toggleStyle(PillToggleStyle(tint: Format.violet))
                .labelsHidden()
        }
    }
}

private struct SettingLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 8) {
            configuration.icon
                .foregroundStyle(.secondary)
                .frame(width: 18)
            configuration.title
        }
    }
}
