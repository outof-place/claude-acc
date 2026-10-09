import Charts
import SwiftUI

// MARK: - Stay Awake

struct AwakeCard: View {
    let awake: Awake
    var store: Store?
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
                        .contentTransition(.numericText())
                }
                HStack(spacing: 6) {
                    ForEach(Self.presets, id: \.label) { preset in
                        Button(preset.label) { awake.stayAwake(for: preset.seconds) }
                            .panelButton(prominent: selected(preset.seconds))
                            .controlSize(.small)
                            .help(preset.seconds == nil ? "Until you turn it off" : "For \(preset.label)")
                    }
                }
                VStack(spacing: 9) {
                    SettingRow("Keep the display on", symbol: "display", isOn: binding(\.keepDisplayOn))
                    SettingRow("Awake with the lid closed", symbol: "laptopcomputer", isOn: binding(\.lidClosed))
                    SettingRow("Auto on the iPhone hotspot", symbol: "personalhotspot", isOn: binding(\.autoOnHotspot))
                    SettingRow("Keep the hotspot alive", symbol: "antenna.radiowaves.left.and.right", isOn: binding(\.keepHotspotAlive))
                    if let store {
                        SettingRow("Hotspot turbo", symbol: "gauge.with.dots.needle.67percent", isOn: Binding(
                            get: { store.hotspotEnabled }, set: { store.setHotspot($0) }))
                            .help("Shapes uploads just under the iPhone's uplink, so one session's upload doesn't queue everyone else's requests")
                    }
                }
                hotspotLine
                if let store, store.hotspotEnabled { turboLine(store) }
            }
        } accessory: {
            Toggle("Stay awake", isOn: Binding(
                get: { awake.isOn },
                set: { $0 ? awake.stayAwake(for: nil) : awake.turnOff() }))
                .toggleStyle(PillToggleStyle(tint: Format.violet))
                .labelsHidden()
        }
        .animation(.smooth(duration: 0.3), value: awake.isOn)
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

    @ViewBuilder private func turboLine(_ store: Store) -> some View {
        HStack(spacing: 6) {
            let state = store.hotspotLive
            StatusDot(color: state?.active == true ? (state?.shaping == false ? .orange : Format.violet) : .gray, size: 7)
            if !store.hotspotInstalled {
                Text("Turbo needs its root helper: claude-acc hotspot install")
            } else if let state, state.active, let rate = state.rateKbps {
                let queue = state.delayP90Ms.map { " · queue p90 \(Int($0.rounded())) ms" } ?? ""
                Text(state.shaping == false
                     ? "Turbo: probes lost, upload unshaped"
                     : "Turbo: upload \(String(format: "%.1f", Double(rate) / 1000)) Mb/s cap\(queue)")
            } else if state != nil {
                Text("Turbo: waits for the iPhone hotspot")
            } else {
                Text("Turbo: the root helper isn't answering")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .monospacedDigit()
        .lineLimit(1)
    }

    private func binding(_ key: ReferenceWritableKeyPath<Awake, Bool>) -> Binding<Bool> {
        Binding(get: { awake[keyPath: key] }, set: { awake[keyPath: key] = $0 })
    }
}

// MARK: - Fans

struct FansCard: View {
    let store: Store
    @Environment(\.now) private var now

    /// The daemon writes every 2 seconds; a state much older than that means it isn't running.
    private var live: FanState? {
        guard let state = store.fanState, now.timeIntervalSince1970 - state.at < 15 else { return nil }
        return state
    }

    private static let modes: [(label: String, value: String)] = [
        ("Auto", "auto"), ("50%", "50"), ("75%", "75"), ("Max", "100"),
    ]

    var body: some View {
        Card("Load & Heat", symbol: "thermometer.medium") {
            VStack(alignment: .leading, spacing: 12) {
                LoadMeters(load: store.load)
                if let state = live {
                    readings(state)
                    HStack(spacing: 6) {
                        ForEach(Self.modes, id: \.value) { mode in
                            Button(mode.label) { store.setFanMode(mode.value) }
                                .panelButton(prominent: store.fanMode == mode.value)
                                .controlSize(.small)
                        }
                    }
                    status(state)
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("Fan control isn't installed", systemImage: "fanblades")
                            .font(.callout)
                        Text("It needs a small root helper: run ./install-fans.sh in the claude-acc folder.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        } accessory: {
            if let state = live {
                SpinningFan(rpm: state.fans.map(\.rpm).max() ?? 0)
            }
        }
    }

    @ViewBuilder private func readings(_ state: FanState) -> some View {
        HStack(spacing: 14) {
            ForEach(state.fans) { fan in
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .firstTextBaseline, spacing: 3) {
                        Text(Format.rpm(fan.rpm))
                            .font(.title3.weight(.semibold))
                            .monospacedDigit()
                        Text("rpm").font(.caption2).foregroundStyle(.secondary)
                    }
                    UsageBar(fraction: fan.share, tint: Format.violet, height: 5, live: true)
                    Text(state.fans.count == 2 ? (fan.index == 0 ? "Left" : "Right") : "Fan \(fan.index + 1)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        let sensors = state.sensors ?? [:]
        HStack(spacing: 6) {
            Temperature(label: "P-cores", value: sensors["pcores"] ?? state.cpu)
            Temperature(label: "E-cores", value: sensors["ecores"])
            Temperature(label: "GPU", value: sensors["gpu"] ?? state.gpu)
            Temperature(label: "SSD", value: sensors["ssd"])
            Temperature(label: "Battery", value: sensors["battery"])
        }
        if let history = state.history, history.count > 2 {
            ThermalChart(history: history).equatable()
        }
    }

    @ViewBuilder private func status(_ state: FanState) -> some View {
        Group {
            if let error = state.error {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            } else if state.conflict == true {
                Label("Another app set the fans (Mole?). Pick a mode here to take them back.",
                      systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            } else if state.boosting == true {
                Label("Full speed: a chip passed 95 °C", systemImage: "flame.fill").foregroundStyle(.orange)
            } else if state.mode == nil {
                Text(state.anyManual ? "Held by another app · pick a mode to take over" : "macOS decides · pick a mode to take over")
                    .foregroundStyle(.secondary)
            } else if state.mode == "auto" {
                Text("macOS decides").foregroundStyle(.secondary)
            } else {
                Text("Held at \(state.percent ?? 0)% · full speed above 95 °C").foregroundStyle(.secondary)
            }
        }
        .font(.caption)
        .lineLimit(2)
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// CPU and GPU load like Activity Monitor: all cores, split into performance and efficiency.
private struct LoadMeters: View {
    let load: LoadReading?

    var body: some View {
        HStack(spacing: 14) {
            meter(load?.cpu, label: cpuLabel)
            meter(load?.gpu, label: "GPU")
        }
    }

    private var cpuLabel: String {
        guard let p = load?.pCores, let e = load?.eCores else { return "CPU" }
        return "CPU · P \(Int(p.rounded()))% · E \(Int(e.rounded()))%"
    }

    private func meter(_ value: Double?, label: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(value.map { "\(Int($0.rounded()))" } ?? "-")
                    .font(.title3.weight(.semibold))
                    .monospacedDigit()
                Text("%").font(.caption2).foregroundStyle(.secondary)
            }
            UsageBar(fraction: (value ?? 0) / 100, tint: Self.tint(value), height: 5, live: true)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .monospacedDigit()
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private static func tint(_ value: Double?) -> Color {
        switch value ?? 0 {
        case ..<60: Format.violet
        case ..<85: .orange
        default: .red
        }
    }
}

private struct Temperature: View {
    let label: String
    let value: Double?

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(value.map { "\(Int($0.rounded()))°" } ?? "-")
                .font(.headline)
                .monospacedDigit()
                .foregroundStyle(Self.tint(value))
            Text(label)
                .font(.caption2.weight(.medium))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    static func tint(_ value: Double?) -> Color {
        guard let value else { return .secondary }
        return switch value {
        case ..<70: .primary
        case ..<88: .orange
        default: .red
        }
    }
}

/// CPU and GPU over the last 20 minutes, against the 95 °C line where fixed modes go full speed.
private struct ThermalChart: View, Equatable {
    let history: [[Double]]

    /// The daemon rewrites its file every 2 s but adds a row about every 5: the chart (480
    /// monotone marks) draws again only when the newest row is another one.
    static func == (a: Self, b: Self) -> Bool {
        a.history.count == b.history.count && a.history.last == b.history.last
    }

    private struct Point: Identifiable {
        let id: Int
        let date: Date
        let part: String
        let celsius: Double
    }

    var body: some View {
        let points = history.enumerated().flatMap { index, row -> [Point] in
            guard row.count >= 3 else { return [] }
            let date = Date(timeIntervalSince1970: row[0])
            // a row's time in tenths of a second names its point: cheap, and stable as rows scroll
            let key = Int(row[0] * 10) * 2
            return [
                Point(id: key, date: date, part: "CPU", celsius: row[1]),
                Point(id: key + 1, date: date, part: "GPU", celsius: row[2]),
            ]
        }
        Chart {
            ForEach(points) { point in
                LineMark(x: .value("Time", point.date), y: .value("°C", point.celsius))
                    .foregroundStyle(by: .value("Part", point.part))
                    .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round))
                    .interpolationMethod(.monotone)
            }
            RuleMark(y: .value("Full speed", 95))
                .foregroundStyle(.red.opacity(0.6))
                .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                .annotation(position: .top, alignment: .leading, spacing: 2) {
                    Text("95°").font(.system(size: 9, weight: .medium)).foregroundStyle(.red.opacity(0.8))
                }
        }
        .chartForegroundStyleScale(["CPU": Color.orange, "GPU": Format.violet])
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(position: .trailing, values: [40, 70, 100]) { value in
                AxisValueLabel { Text("\(value.as(Int.self) ?? 0)°").font(.system(size: 9)) }
            }
        }
        .chartYScale(domain: 30...110)
        .chartLegend(position: .bottom, alignment: .leading, spacing: 4)
        // the last card of its column: the chart gives up height before the card spills out
        .frame(minHeight: 44, idealHeight: 92, maxHeight: 92)
        .accessibilityLabel("CPU and GPU temperature over the last 20 minutes")
    }
}

/// The fan glyph turns, faster as the fans do. Core Animation turns it in the render server:
/// a symbol effect made SwiftUI redraw the whole panel on every frame while the fans ran.
private struct SpinningFan: View {
    let rpm: Double
    @Environment(\.animating) private var animating
    @Environment(\.renderingToFile) private var renderingToFile

    var body: some View {
        Group {
            if renderingToFile {
                // ImageRenderer draws no AppKit views
                Image(systemName: "fanblades.fill")
                    .font(.callout)
                    .foregroundStyle(Format.violet)
            } else {
                TurningSymbol(
                    name: "fanblades.fill", tint: NSColor(Format.violet),
                    turnsPerSecond: animating ? Self.turns(rpm) : 0)
            }
        }
        .help("\(Int(rpm)) rpm")
    }

    /// One turn a second at 2,500 rpm, in quarter steps: the few rpm the fans wobble by
    /// between readings never retime the turn.
    static func turns(_ rpm: Double) -> Double {
        rpm > 0 ? (max(rpm / 2500, 0.2) * 4).rounded() / 4 : 0
    }
}

// MARK: - Shared

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
