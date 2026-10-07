import SwiftUI

/// The menu bar panel: Claude accounts, dev servers and disk, then the Mac itself, and one footer bar.
/// Wide rather than tall so it fits under the menu bar of a laptop, and a fixed height:
/// what grows (account details, long server lists) scrolls inside its card, so the window
/// never jumps in size while you click around.
struct PanelView: View {
    let store: Store
    /// A fixed clock for rendering a snapshot file: times read as they did when it was taken.
    var frozenNow: Date?
    @Environment(\.renderingToFile) private var renderingToFile

    static let columnHeight: CGFloat = 640

    var body: some View {
        TimelineView(PanelClock(running: store.panelOpen)) { context in
            VStack(spacing: 12) {
                if let email = store.snapshot?.orcaSelected {
                    Banner(
                        symbol: "exclamationmark.triangle.fill", tint: .orange,
                        title: "Orca has \(email) selected",
                        text: "Orca reverts switches and refreshes tokens itself, and two refreshers sign accounts out. Auto-switch and switching are paused until you pick System default in Orca's Claude account menu.")
                }
                HStack(alignment: .top, spacing: 12) {
                    Column(width: 350) {
                        ActiveAccountCard(store: store).fixedSize(horizontal: false, vertical: true)
                        AccountsCard(store: store)
                        DiskCard(store: store).fixedSize(horizontal: false, vertical: true)
                    }
                    Column(width: 360) {
                        DevServersCard(store: store)
                        BuildsCard(store: store)
                        MailCard(store: store).fixedSize(horizontal: false, vertical: true)
                    }
                    Column(width: 310) {
                        AwakeCard(awake: store.awake).fixedSize(horizontal: false, vertical: true)
                        if store.browser?.installed == true {
                            BrowserCard(store: store).fixedSize(horizontal: false, vertical: true)
                        }
                        FansCard(store: store)
                    }
                    Column(width: 300) {
                        UltraCard(store: store)
                        UpdatesCard(store: store).fixedSize(horizontal: false, vertical: true)
                    }
                }
                // live: fixed height, lists scroll inside their cards; PNG: as tall as the content
                .frame(height: renderingToFile ? nil : Self.columnHeight)
                .frame(minHeight: renderingToFile ? Self.columnHeight : nil)
                .fixedSize(horizontal: false, vertical: renderingToFile)
                if let notice = store.notice {
                    Banner(
                        symbol: notice.isError ? "xmark.octagon.fill" : "checkmark.circle.fill",
                        tint: notice.isError ? .red : .green, title: notice.text
                    ) { store.notice = nil }
                }
                if let problem = store.problem {
                    Banner(symbol: "exclamationmark.octagon.fill", tint: .red, title: problem)
                }
                FooterBar(store: store)
            }
            .padding(14)
            .environment(\.now, frozenNow ?? context.date)
            .environment(\.animating, store.panelOpen)
        }
        .fontDesign(.rounded)
        .animation(.smooth(duration: 0.35), value: store.notice)
    }
}

/// Every 5 seconds while the panel is open; closed, one entry and no more updates.
private struct PanelClock: TimelineSchedule {
    let running: Bool

    func entries(from start: Date, mode: TimelineScheduleMode) -> AnyIterator<Date> {
        var next: Date? = start
        let running = running
        return AnyIterator {
            defer { next = running ? next?.addingTimeInterval(5) : nil }
            return next
        }
    }
}

/// A column of cards filling the panel's height: the last card stretches, so every column
/// ends on the same line.
private struct Column<Content: View>: View {
    let width: CGFloat
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 12) {
            content
        }
        .frame(width: width)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

private struct FooterBar: View {
    let store: Store
    @Environment(\.now) private var now
    @Environment(\.animating) private var animating

    var body: some View {
        HStack(spacing: 14) {
            autoSwitch
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                Text("Updated \(Format.ago(store.snapshot?.generatedAt, now: now))")
                    .foregroundStyle(.secondary)
                Button("Refresh", systemImage: "arrow.clockwise") { Task { await store.refresh() } }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .symbolEffect(.rotate, options: .repeat(.continuous), isActive: store.refreshing && animating)
                    .disabled(store.refreshing)
                    .help("Refresh")
            }
            Divider().frame(height: 14)
            Toggle("Open at Login", isOn: Binding(
                get: { store.launchAtLogin },
                set: { store.setLaunchAtLogin($0) }))
                .toggleStyle(.pill)
            Menu("Logs") {
                Button("Switch History", systemImage: "arrow.triangle.swap") { open(CLI.switchLog) }
                Button("Cleanup Log", systemImage: "doc.text") { open(CLI.janitorLog) }
                Button("Dev Server Guard Log", systemImage: "server.rack") { open(CLI.guardLog) }
                Button("Updates Log", systemImage: "arrow.down.circle") { open(CLI.updatesLog) }
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .fixedSize()
            Button("Quit") { NSApp.terminate(nil) }
                .buttonStyle(.plain)
                .keyboardShortcut("q")
        }
        .font(.caption)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.quinary, in: .capsule)
    }

    @ViewBuilder private var autoSwitch: some View {
        if let snapshot = store.snapshot {
            let tickAge = snapshot.lastTick.map { now.timeIntervalSince1970 - $0 }
            let healthy = (tickAge ?? .infinity) < 600 && snapshot.orcaSelected == nil
            let t = snapshot.thresholds
            HStack(spacing: 7) {
                StatusDot(color: healthy ? .green : .orange, size: 7)
                Group {
                    if let until = snapshot.apiBackoffUntil, until > now.timeIntervalSince1970 {
                        Text("Usage API rate-limited until \(Format.moment(until, now: now))")
                            .foregroundStyle(.orange)
                    } else if snapshot.orcaSelected != nil {
                        Text("Auto-switch paused by Orca")
                    } else if snapshot.lastTick == nil {
                        Text("Auto-switch hasn't run yet")
                    } else if healthy {
                        Text("Auto-switch checked \(Format.ago(snapshot.lastTick, now: now))")
                    } else {
                        Text("Auto-switch silent since \(Format.ago(snapshot.lastTick, now: now))")
                    }
                }
                .lineLimit(1)
            }
            .help("Switches when \(Int(t.sessionLeft))% of the session or \(Int(t.weeklyLeft))% of the week is left")
        }
    }

    private func open(_ path: String) {
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }
}
