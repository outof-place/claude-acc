import AppKit
import SwiftUI

/// The menu bar panel: Claude accounts, dev servers and disk, then the Mac itself, and one footer bar.
/// Wide rather than tall so it fits under the menu bar of a laptop, and a fixed height:
/// what grows (account details, long server lists) scrolls inside its card, so the window
/// never jumps in size while you click around.
///
/// This is the whole panel as one view, for a PNG. On screen PanelWindow puts its parts (the top
/// banner, each column, the bottom) in NSHostingViews of their own: an update costs SwiftUI in
/// proportion to the graph it runs in (the key view loop, dirtying, the display list), and with one
/// graph for everything the builds' progress every second ran the whole panel. Open, that was
/// 53 ms/s of CPU; a host per column is 24 (--measure-panel, 2026-10-11). Hosts of their own for
/// the busiest cards (Builds, Load & Heat) inside a column cost more than they saved: 29.
struct PanelView: View {
    let store: Store
    /// A fixed clock for rendering a snapshot file: times read as they did when it was taken.
    var frozenNow: Date?
    @Environment(\.renderingToFile) private var renderingToFile

    static let padding: CGFloat = 14

    /// As tall as the screen under the menu bar allows (the padding, the footer and a little
    /// air taken off), between 760 and 1000: the cards that grew since 760 (Stay Awake's hotspot
    /// rows, Dictation) fit on a laptop without scrolling. A PNG render keeps 760.
    static var columnHeight: CGFloat {
        let screen = NSScreen.main?.visibleFrame.height ?? 900
        return min(1000, max(760, screen - 112))
    }

    var body: some View {
        PanelPart(store: store, frozenNow: frozenNow) {
            VStack(spacing: 0) {
                PanelTop(store: store)
                HStack(alignment: .top, spacing: PanelColumn.spacing) {
                    ForEach(PanelColumn.allCases, id: \.self) { PanelColumnView(column: $0, store: store) }
                }
                // live: fixed height, lists scroll inside their cards; PNG: as tall as the content
                .frame(height: renderingToFile ? nil : Self.columnHeight)
                .frame(minHeight: renderingToFile ? 760 : nil)
                .fixedSize(horizontal: false, vertical: renderingToFile)
                PanelBottom(store: store)
            }
            .padding(Self.padding)
        }
    }
}

/// The clock and the environment every part of the panel reads, around one part or all of them.
struct PanelPart<Content: View>: View {
    let store: Store
    var frozenNow: Date?
    @ViewBuilder let content: Content

    var body: some View {
        TimelineView(PanelClock(running: store.panelOpen)) { context in
            content
                .environment(\.now, frozenNow ?? context.date)
                .environment(\.animating, store.panelOpen)
        }
        .fontDesign(.rounded)
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

/// Above the columns: the host app has an account selected, which pauses switching.
struct PanelTop: View {
    let store: Store

    var body: some View {
        if let email = store.snapshot?.orcaSelected {
            Banner(
                symbol: "exclamationmark.triangle.fill", tint: .orange,
                title: "\(HostApp.current.name) has \(email) selected",
                text: "\(HostApp.current.name) reverts switches and refreshes tokens itself, and two refreshers sign accounts out. Auto-switch and switching are paused until you pick System default in \(HostApp.current.name)'s Claude account menu.")
                .padding(.bottom, PanelColumn.spacing)
        }
    }
}

/// Under the columns: what the last action did, what went wrong, and the footer bar.
struct PanelBottom: View {
    let store: Store

    var body: some View {
        VStack(spacing: 12) {
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
        .padding(.top, PanelColumn.spacing)
        .animation(.smooth(duration: 0.35), value: store.notice)
    }
}

/// The panel's columns, left to right; on screen each one is its own NSHostingView.
enum PanelColumn: CaseIterable {
    case accounts, work, machine, tools

    static let spacing: CGFloat = 12

    var width: CGFloat {
        switch self {
        case .accounts: 350
        case .work: 360
        case .machine: 310
        case .tools: 300
        }
    }

    /// The columns side by side, with the padding around them.
    static var panelWidth: CGFloat {
        allCases.reduce(0) { $0 + $1.width } + spacing * CGFloat(allCases.count - 1) + 2 * PanelView.padding
    }
}

struct PanelColumnView: View {
    let column: PanelColumn
    let store: Store

    var body: some View {
        Column(width: column.width) {
            switch column {
            case .accounts:
                ActiveAccountCard(store: store).fixedSize(horizontal: false, vertical: true)
                AccountsCard(store: store)
                if let credits = store.snapshot?.credits {
                    CreditsCard(credits: credits).fixedSize(horizontal: false, vertical: true)
                }
                DiskCard(store: store).fixedSize(horizontal: false, vertical: true)
            case .work:
                DevServersCard(store: store)
                BuildsCard(store: store)
                MailCard(store: store).fixedSize(horizontal: false, vertical: true)
            case .machine:
                AwakeCard(awake: store.awake, store: store).fixedSize(horizontal: false, vertical: true)
                if store.browser?.installed == true {
                    BrowserCard(store: store).fixedSize(horizontal: false, vertical: true)
                }
                FansCard(store: store)
            case .tools:
                // Ultra scrolls inside its card, so this column absorbs the Desktop card;
                // the third one cannot: Load & Heat has a fixed chart and would spill out.
                DictationCard(dictation: store.dictation).fixedSize(horizontal: false, vertical: true)
                UltraCard(store: store)
                if store.desktop?.installed == true {
                    DesktopCard(store: store).fixedSize(horizontal: false, vertical: true)
                }
                UpdatesCard(store: store).fixedSize(horizontal: false, vertical: true)
            }
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
            // Pod Menu starts with Pod (its login item), so there is no switch of its own
            if !PodMenu.active {
                Toggle("Open at Login", isOn: Binding(
                    get: { store.launchAtLogin },
                    set: { store.setLaunchAtLogin($0) }))
                    .toggleStyle(.pill)
            }
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
                        Text("Auto-switch paused by \(HostApp.current.name)")
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
