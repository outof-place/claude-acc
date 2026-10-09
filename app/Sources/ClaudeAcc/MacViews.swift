import Charts
import SwiftUI

// MARK: - Dev servers

struct DevServersCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Dev Servers", symbol: "server.rack") {
            if let state = store.guardState, let snapshot = state.snapshot,
               now.timeIntervalSince1970 - snapshot.at < 90 {
                GuardContent(store: store, state: state, snapshot: snapshot)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Label("The guard isn't running", systemImage: "pause.circle")
                        .font(.callout)
                    Text("launchd job com.filip.claude-acc.devguard · `claude-acc guard status`")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        } accessory: {
            if store.guardState?.snapshot != nil {
                Toggle("Auto", isOn: Binding(
                    get: { store.guardEnforcing },
                    set: { store.setGuardEnforcing($0) }))
                    .toggleStyle(.pill)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .help("On: the guard restarts bloated servers and stops abandoned ones. Off: it only watches.")
            }
        }
    }
}

private struct GuardContent: View {
    let store: Store
    let state: GuardState
    let snapshot: GuardSnapshot
    @Environment(\.now) private var now

    var body: some View {
        let pressure = Format.pressure(snapshot.pressure.level)
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(Format.bytes(snapshot.total))
                    .font(.title2.weight(.semibold))
                    .monospacedDigit()
                    .contentTransition(.numericText(value: snapshot.total))
                    .foregroundStyle(snapshot.total > snapshot.budget ? .orange : .primary)
                Text("of \(Format.bytes(snapshot.budget)) budget")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Chip(pressure.text, symbol: "memorychip", tint: pressure.color)
                    .help(memoryHelp)
            }
            if let history = state.history, history.count > 2 {
                MemoryChart(history: history, budget: snapshot.budget).equatable()
            }
            if snapshot.units.isEmpty {
                Label("No dev servers running", systemImage: "checkmark.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                CardScroll {
                    VStack(spacing: 2) {
                        ForEach(snapshot.units.sorted { $0.footprint > $1.footprint }) { unit in
                            ServerRow(store: store, unit: unit, plan: snapshot.plan(for: unit))
                                .transition(.blurReplace)
                        }
                    }
                }
                .padding(.horizontal, -6)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(swapLine)
                if let event = state.events?.last {
                    EventLine(event: event)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .animation(.smooth, value: snapshot.units.map(\.key))
    }

    private var swapLine: String {
        let p = snapshot.pressure
        var text = "Swap \(Format.bytes(p.swapUsed))"
        if p.swapping {
            text += " · growing"
        } else if !(p.notes ?? []).isEmpty {
            text += " · settled"
        }
        return text + " · compressed \(Format.bytes(p.compressed))"
    }

    private var memoryHelp: String {
        let p = snapshot.pressure
        let available = p.available.map { "\($0)% of memory available" } ?? ""
        return "\(available). The guard acts on growing swap, not on macOS's pressure level, which stays normal until jetsam kills apps."
    }
}

private struct EventLine: View {
    let event: GuardEvent
    @Environment(\.now) private var now

    var body: some View {
        let port = event.ports?.first.map { ":\($0)" } ?? event.label
        let restart = event.action == "restart"
        let what = event.ok ? (restart ? "Restarted" : "Stopped") : (restart ? "Couldn't restart" : "Couldn't stop")
        let why = Format.reason(event.code, event.data)
        let parts = ["\(what) \(port)", why, Format.ago(event.at, now: now)].filter { !$0.isEmpty }
        HStack(spacing: 5) {
            Image(systemName: event.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(event.ok ? .green : .red)
            Text(parts.joined(separator: " · "))
        }
        .lineLimit(1)
        .truncationMode(.middle)
    }
}

private struct ServerRow: View {
    let store: Store
    let unit: GuardUnit
    let plan: GuardPlan?
    @State private var hovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            StatusDot(color: dotColor)
                .padding(.top, 6)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(unit.title)
                        .font(.callout.weight(.medium))
                        .lineLimit(1)
                    Text(unit.portLabel)
                        .font(.caption.weight(.medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 4)
                    if store.guardBusy == unit.key {
                        ProgressView().controlSize(.mini)
                    }
                    Text(Format.bytes(unit.footprint))
                        .font(.callout.weight(.semibold))
                        .monospacedDigit()
                        .contentTransition(.numericText(value: unit.footprint))
                        .foregroundStyle(unit.footprint >= 5 * gigabyte ? .orange : .primary)
                }
                HStack(spacing: 6) {
                    Text(unit.place)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .layoutPriority(-1)
                    viewers
                    if unit.agentWorking {
                        Image(systemName: "hammer.fill")
                            .foregroundStyle(Format.violet)
                            .help("An agent is working in this worktree")
                    }
                    if unit.background == true {
                        Image(systemName: "leaf.fill")
                            .foregroundStyle(.green.opacity(0.8))
                            .help("Low priority while you aren't looking at it: efficiency cores, throttled disk")
                    }
                    if unit.protected {
                        Image(systemName: "lock.fill")
                            .foregroundStyle(.secondary)
                            .help("Protected in devguard.json, the guard never touches it")
                    }
                }
                .font(.caption)
                if let plan {
                    Label(planText(plan), systemImage: plan.action == "warn" ? "exclamationmark.triangle" : "clock.arrow.circlepath")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .lineLimit(1)
                }
            }
            actions
                .opacity(hovering ? 1 : 0.35)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .background(hovering ? AnyShapeStyle(.quaternary) : AnyShapeStyle(.clear), in: .rect(cornerRadius: 14, style: .continuous))
        .contentShape(.rect(cornerRadius: 14, style: .continuous))
        .onHover { hovering = $0 }
        .animation(.snappy(duration: 0.18), value: hovering)
        .help(unit.command.map { "\($0)\n\(unit.terminal.map { "Orca terminal “\($0)”" } ?? unit.host)" } ?? "")
    }

    private var dotColor: Color {
        if plan != nil { return .orange }
        if unit.attended { return .green }
        return unit.background == true ? .gray : Format.violet
    }

    @ViewBuilder private var viewers: some View {
        switch unit.viewers {
        case .you: Chip("You", symbol: "eye.fill", tint: .green)
        case .agents: Chip("Agent preview", symbol: "eye")
        case .headless: Chip("Headless", symbol: "theatermasks")
        case .nobody: Chip("No viewers", symbol: "eye.slash")
        }
    }

    private func planText(_ plan: GuardPlan) -> String {
        let why = Format.reason(plan.code, plan.data)
        return switch plan.action {
        case "recycle": "Restart when quiet · \(why)"
        case "stop": "Will stop · \(why)"
        default: why.prefix(1).uppercased() + why.dropFirst()
        }
    }

    private var actions: some View {
        Menu {
            Button("Restart", systemImage: "arrow.clockwise") { Task { await store.restart(unit) } }
                .disabled(!unit.recyclable)
            Button("Stop", systemImage: "stop.fill", role: .destructive) { Task { await store.stop(unit) } }
            Divider()
            Button("Open in Browser", systemImage: "safari") { store.openInBrowser(unit) }
                .disabled(unit.ports.isEmpty)
            Button("Copy Start Command", systemImage: "doc.on.doc") { store.copyCommand(unit) }
                .disabled(unit.command == nil)
        } label: {
            Image(systemName: "ellipsis.circle.fill")
                .font(.title3)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.secondary)
        }
        .menuStyle(.button)
        .menuIndicator(.hidden)
        .buttonStyle(.plain)
        .fixedSize()
        .disabled(store.guardBusy != nil)
    }
}

/// Dev server memory over the last two hours against the budget line.
private struct MemoryChart: View, Equatable {
    let history: [[Double]]
    let budget: Double

    /// Redrawn only when a row comes or the budget moves, not on every rewrite of the file.
    static func == (a: Self, b: Self) -> Bool {
        a.budget == b.budget && a.history.count == b.history.count && a.history.last == b.history.last
    }

    private struct Point: Identifiable {
        let date: Date
        let devServers: Double
        let swap: Double
        var id: Date { date }
    }

    var body: some View {
        let points = history.compactMap { row -> Point? in
            guard row.count >= 3 else { return nil }
            return Point(
                date: Date(timeIntervalSince1970: row[0]),
                devServers: row[1] / gigabyte, swap: row[2] / gigabyte)
        }
        let top = max(points.map(\.devServers).max() ?? 0, budget / gigabyte) * 1.12
        Chart {
            ForEach(points) { point in
                AreaMark(x: .value("Time", point.date), y: .value("Dev servers", point.devServers))
                    .foregroundStyle(
                        .linearGradient(
                            colors: [Format.violet.opacity(0.45), Format.violet.opacity(0.02)],
                            startPoint: .top, endPoint: .bottom))
                    .interpolationMethod(.monotone)
                LineMark(x: .value("Time", point.date), y: .value("Dev servers", point.devServers))
                    .foregroundStyle(Format.violet)
                    .lineStyle(StrokeStyle(lineWidth: 1.6, lineCap: .round))
                    .interpolationMethod(.monotone)
            }
            RuleMark(y: .value("Budget", budget / gigabyte))
                .foregroundStyle(.orange.opacity(0.7))
                .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                .annotation(position: .top, alignment: .leading, spacing: 2) {
                    Text("budget").font(.system(size: 9, weight: .medium)).foregroundStyle(.orange.opacity(0.8))
                }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartYScale(domain: 0...top)
        .chartLegend(.hidden)
        .frame(height: 58)
        .accessibilityLabel("Dev server memory over the last two hours")
    }
}

// MARK: - Disk and cleanup

struct DiskCard: View {
    let store: Store
    @Environment(\.now) private var now

    var body: some View {
        Card("Disk", symbol: "internaldrive") {
            VStack(alignment: .leading, spacing: 10) {
                if let disk = store.disk {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(Format.bytes(disk.free))
                            .font(.title2.weight(.semibold))
                            .monospacedDigit()
                            .contentTransition(.numericText(value: disk.free))
                        Text("free of \(Format.bytes(disk.total))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    UsageBar(fraction: disk.used, tint: Format.tint(disk.used * 100))
                }
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(alerts, id: \.self) { alert in
                    AlertRow(store: store, alert: alert)
                }
            }
        } accessory: {
            if running {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Cleaning…").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Button("Clean Up", systemImage: "trash") { Task { await store.sweep() } }
                    .panelButton()
                    .controlSize(.small)
                    .help("Delete unused build caches, stale node_modules and tool leftovers")
            }
        }
    }

    /// The last sweep's alerts; the low-disk one stays only while today's reading is still low.
    private var alerts: [JanitorState.Alert] {
        (store.janitor?.alerts ?? []).filter { $0.kind != "low_disk" || $0.lowDiskFree(now: store.disk) != nil }
    }

    private var running: Bool {
        if store.sweeping { return true }
        guard let since = store.janitor?.runningSince else { return false }
        return now.timeIntervalSince1970 - since < 2 * 3600
    }

    private var summary: String {
        guard let last = store.janitor?.lastSweep else { return "No cleanup yet" }
        var text = "Cleaned \(Format.ago(last.at, now: now)) · freed \(Format.bytes(last.freed))"
        if let total = store.janitor?.freedTotal, total > last.freed {
            text += " · \(Format.bytes(total)) in total"
        }
        return text
    }
}

private struct AlertRow: View {
    let store: Store
    let alert: JanitorState.Alert

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Label(text, systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            if alert.kind == "spotlight" {
                Button("Fix") { Task { await store.openSpotlightSettings() } }
                    .panelButton()
                    .controlSize(.mini)
            }
        }
    }

    private var text: String {
        switch alert.kind {
        case "low_disk":
            return "Only \(Format.bytes(alert.lowDiskFree(now: store.disk) ?? 0)) left on disk"
        case "spotlight":
            let projects = alert.projects ?? []
            let more = projects.count > 3 ? " and \(projects.count - 3) more" : ""
            return "Spotlight indexes node_modules in \(projects.prefix(3).joined(separator: ", "))\(more)"
        case "task_failed":
            return "Cleanup task \(alert.task ?? "?") failed: \(alert.error ?? "")"
        default:
            return alert.kind
        }
    }
}
