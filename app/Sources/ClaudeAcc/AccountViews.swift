import SwiftUI

// MARK: - Active account

struct ActiveAccountCard: View {
    let store: Store

    var body: some View {
        Card("Claude Code", symbol: "terminal.fill") {
            if let snapshot = store.snapshot {
                if let pause = snapshot.pause {
                    PauseNotice(store: store, pause: pause)
                }
                if let active = snapshot.active {
                    ActiveAccount(store: store, snapshot: snapshot, account: active)
                } else {
                    Text("Claude Code is signed in to an account outside Orca. Auto-switch leaves it alone until you switch to one from the list.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if snapshot.drain != nil || snapshot.limitPause != nil {
                    VStack(spacing: 9) {
                        if let on = snapshot.drain {
                            SettingSwitch(store: store, setting: .drain, on: on)
                        }
                        if let on = snapshot.limitPause {
                            SettingSwitch(store: store, setting: .limitPause, on: on)
                        }
                    }
                }
            } else if store.problem == nil {
                ProgressView("Reading account limits…").controlSize(.small)
            } else {
                Text("No account data yet").font(.callout).foregroundStyle(.secondary)
            }
        } accessory: {
            if let tier = store.snapshot?.active?.tier, !tier.isEmpty {
                Chip(tier, tint: Format.violet)
            }
        }
    }
}

private struct ActiveAccount: View {
    let store: Store
    let snapshot: Snapshot
    let account: Account
    @State private var showDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Button {
                showDetails.toggle()
            } label: {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(account.email)
                            .font(.callout.weight(.medium))
                            .lineLimit(1)
                            .truncationMode(.middle)
                        SubscriptionText(account: account)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if account.mislabeled, let real = account.realEmail {
                            Label("This Orca entry holds \(real)", systemImage: "exclamationmark.triangle.fill")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                    }
                    Spacer(minLength: 8)
                    DisclosureChevron(open: showDetails)
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .help(showDetails ? "Hide details" : "Show details")
            if showDetails {
                AccountDetails(store: store, snapshot: snapshot, account: account)
                    .transition(.blurReplace.combined(with: .move(edge: .top)))
            }
            if account.status == .needsLogin {
                LoginPrompt(store: store, account: account)
            } else {
                UsageMeter(
                    title: "Session", symbol: "clock", window: account.session,
                    forecast: snapshot.forecast?.session,
                    threshold: snapshot.thresholds.sessionLeft, stale: account.status != .ok)
                UsageMeter(
                    title: "Weekly", symbol: "calendar", window: account.weekly,
                    forecast: snapshot.forecast?.weekly,
                    threshold: snapshot.thresholds.weeklyLeft, stale: account.status != .ok)
                if account.status == .error {
                    Text(account.note).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .animation(.smooth(duration: 0.32), value: showDetails)
    }
}

/// A watcher setting as one row of the card, in the order it acts when limits run out:
/// first the last few percent of every account, then the pause. The explanation sits in
/// the tooltip, so each switch costs the card one row.
private struct SettingSwitch: View {
    let store: Store
    let setting: Store.Setting
    let on: Bool

    var body: some View {
        SettingRow(title, symbol: symbol, isOn: Binding(
            get: { store.settingPicks[setting] ?? on },
            set: { value in Task { await store.set(setting, value) } }))
            .disabled(store.settingBusy != nil)
            .help(help)
    }

    private var title: String {
        switch setting {
        case .drain: "Use up every account"
        case .limitPause: "Pause at the limit"
        }
    }

    private var symbol: String {
        switch setting {
        case .drain: "battery.25percent"
        case .limitPause: "pause.circle"
        }
    }

    private var help: String {
        switch setting {
        case .drain:
            "When no account has headroom, the active one works to its last percent, then sessions move through whatever is left on the others, one account after another."
        case .limitPause:
            "When no account has headroom, sessions finish their step at a checkpoint and wake when limits return. Off: they work until the limit and Claude Code resumes them after the reset."
        }
    }
}

/// The limit pause: every account is out of headroom, so the hooks told the running sessions
/// to finish their step, save their state and wait. They wake on their own when limits recover;
/// the button lifts the pause sooner, until limits recover and run out again.
private struct PauseNotice: View {
    let store: Store
    let pause: Pause
    @Environment(\.now) private var now

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "pause.circle.fill")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Sessions paused at a checkpoint").font(.callout.weight(.semibold))
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack {
                Text("Since \(Format.moment(pause.since, now: now))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Resume Now", systemImage: "play.fill") { Task { await store.resumePaused() } }
                    .panelButton()
                    .controlSize(.small)
                    .disabled(store.resuming)
                    .help("Wake the paused sessions now. The pause comes back only after limits recover and run out again.")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: .rect(cornerRadius: 16, style: .continuous))
    }

    private var detail: String {
        let wake = pause.resumeAt.map {
            "They wake up on their own around \(Format.moment($0, now: now)) (\(Format.until($0, now: now)))."
        } ?? "They wake up on their own once an account has headroom again."
        return "No account has headroom. Sessions finish their current step, save their state to TASKS.md and wait, and new subagents are held. "
            + wake
    }
}

/// Chevron that turns down when its section is open.
struct DisclosureChevron: View {
    let open: Bool

    var body: some View {
        Image(systemName: "chevron.right")
            .font(.caption.weight(.bold))
            .foregroundStyle(.tertiary)
            .rotationEffect(.degrees(open ? 90 : 0))
            .animation(.snappy(duration: 0.2), value: open)
            .frame(width: 14, height: 14)
    }
}

/// Everything known about one account, to the minute.
struct AccountDetails: View {
    let store: Store
    let snapshot: Snapshot
    let account: Account
    @Environment(\.now) private var now

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 7) {
                row("Plan", account.tier.isEmpty ? "Unknown" : account.tier)
                row("Subscription", subscription.status, detail: subscription.since,
                    tint: account.subscriptionStatus.map { $0 == "active" ? nil : .orange } ?? nil)
                if let renews = account.renewsAt {
                    row("Renews", Format.fullDate(renews), detail: "in \(Format.countdown(renews, now: now))",
                        help: "Estimated: the monthly anniversary of the subscription start. The API has no billing date.")
                }
                window("5-hour", account.session)
                window("Weekly", account.weekly)
                row("Auto-switch", queue)
                if account.active, let switched = snapshot.switchedAt {
                    row("Active since", Format.stamp(switched, now: now), detail: Format.ago(switched, now: now))
                }
                if let age = account.dataAge {
                    row("Data", "read \(Format.age(age)) ago", tint: age > 900 ? .orange : nil)
                }
                if account.mislabeled, let real = account.realEmail {
                    row("Holds", real, tint: .orange)
                }
                if account.status != .ok, !account.note.isEmpty {
                    row("Note", account.note, tint: .orange)
                }
            }
            .font(.caption)
            if !account.active {
                HStack(spacing: 8) {
                    Button("Switch Here", systemImage: "arrow.triangle.swap") { Task { await store.switchTo(account) } }
                        .panelButton()
                        .disabled(store.busy != nil || account.status == .needsLogin || snapshot.orcaSelected != nil)
                    Button("Sign In Again", systemImage: "person.badge.key") { Task { await store.login(account) } }
                        .panelButton()
                        .disabled(store.busy != nil)
                }
                .controlSize(.small)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 14, style: .continuous))
    }

    private var subscription: (status: String, since: String?) {
        let status = (account.subscriptionStatus ?? "unknown").replacingOccurrences(of: "_", with: " ")
        return (status.capitalized, account.subscriptionSince.map { "since \(Format.isoDay($0))" })
    }

    private var queue: String {
        if account.active { return "Active now" }
        if account.status == .needsLogin { return "Skipped until it signs in again" }
        if let status = account.subscriptionStatus, status != "active" { return "Skipped, subscription \(status)" }
        if account.lastResort { return account.queue.map { "#\($0) in line · backup, used last" } ?? "Backup, used last" }
        return account.queue.map { "#\($0) in line" } ?? (account.usable ? "In line" : "Skipped, no headroom")
    }

    /// Label, value and an optional quieter second line; one line each, the tooltip has the rest.
    @ViewBuilder
    private func row(
        _ label: String, _ value: String, detail: String? = nil, tint: Color? = nil, help: String? = nil
    ) -> some View {
        GridRow(alignment: .firstTextBaseline) {
            Text(label)
                .foregroundStyle(.secondary)
                .gridColumnAlignment(.trailing)
            VStack(alignment: .leading, spacing: 1) {
                Text(value)
                    .foregroundStyle(tint ?? .primary)
                if let detail {
                    Text(detail)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            .lineLimit(1)
            .help(help ?? [value, detail].compactMap(\.self).joined(separator: " · "))
        }
    }

    @ViewBuilder
    private func window(_ label: String, _ window: UsageWindow?) -> some View {
        if let window {
            let used = "\(Format.percent(window.used)) used"
            let tint: Color? = (window.used ?? 0) >= 90 ? .red : nil
            if let reset = window.resetsAt {
                row(label, "\(used) · resets \(Format.stamp(reset, now: now))",
                    detail: "in \(Format.countdown(reset, now: now))", tint: tint)
            } else {
                row(label, used, detail: "not started, starts on first use", tint: tint)
            }
        } else {
            row(label, "No data")
        }
    }
}

private struct UsageMeter: View {
    let title: String
    let symbol: String
    let window: UsageWindow?
    let forecast: WindowForecast?
    let threshold: Double
    let stale: Bool
    @Environment(\.now) private var now

    var body: some View {
        let used = window?.used
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Label(title, systemImage: symbol)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Spacer()
                Text(Format.percent(used))
                    .font(.title3.weight(.semibold))
                    .monospacedDigit()
                    .contentTransition(.numericText(value: used ?? 0))
                    .foregroundStyle(stale ? .secondary : Format.tint(used))
            }
            UsageBar(
                fraction: (used ?? 0) / 100, tint: stale ? .secondary : Format.tint(used),
                marker: 1 - threshold / 100)
            HStack {
                forecastText
                Spacer()
                if let reset = window?.resetsAt {
                    Text("Resets \(Format.moment(reset, now: now)) · \(Format.countdown(reset, now: now))")
                        .help("Resets \(Format.stamp(reset, now: now))")
                } else {
                    Text(window == nil ? "No data" : "Window not started")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var forecastText: some View {
        if let switchAt = forecast?.switchAt {
            Label("Switches ~\(Format.moment(switchAt, now: now))", systemImage: "arrow.triangle.swap")
                .foregroundStyle(.orange)
        } else if let forecast {
            Text("On pace · ~\(Int(forecast.atReset.rounded()))% at reset")
        }
    }
}

private struct LoginPrompt: View {
    let store: Store
    let account: Account

    var body: some View {
        HStack {
            if store.busy == .loggingIn(account.email) {
                Label("Finish signing in in your browser", systemImage: "safari")
                    .foregroundStyle(.orange)
                Spacer()
                Button("Cancel") { store.cancelLogin() }
                    .panelButton()
            } else {
                Label("Session expired", systemImage: "person.crop.circle.badge.exclamationmark")
                    .foregroundStyle(.orange)
                Spacer()
                Button("Sign In") { Task { await store.login(account) } }
                    .panelButton(prominent: true)
                    .disabled(store.busy != nil)
            }
        }
        .font(.callout)
    }
}

/// The next renewal, or the subscription state once it stops being active.
private struct SubscriptionText: View {
    let account: Account

    var body: some View {
        if let status = account.subscriptionStatus, status != "active" {
            Text("Subscription \(status.replacingOccurrences(of: "_", with: " ")) · skipped")
                .foregroundStyle(.orange)
        } else if let renews = account.renewsAt {
            Text("Renews \(Format.day(renews)) · \(Format.inDays(renews))")
        }
    }
}

// MARK: - Other accounts

struct AccountsCard: View {
    let store: Store
    /// One account open at a time keeps the panel short.
    @State private var expanded: String?

    init(store: Store) {
        self.store = store
        _expanded = State(initialValue: store.previewOpenAccount)
    }

    var body: some View {
        if let snapshot = store.snapshot, !snapshot.others.isEmpty {
            Card("Accounts", symbol: "person.2") {
                CardScroll {
                VStack(spacing: 2) {
                    ForEach(snapshot.others) { account in
                        VStack(spacing: 6) {
                            AccountRow(
                                store: store, account: account,
                                isNext: account.id == snapshot.next?.id,
                                switchBlocked: snapshot.orcaSelected != nil,
                                isOpen: expanded == account.id
                            ) {
                                expanded = expanded == account.id ? nil : account.id
                            }
                            if expanded == account.id {
                                AccountDetails(store: store, snapshot: snapshot, account: account)
                                    .padding(.horizontal, 6)
                                    .padding(.bottom, 6)
                                    .transition(.blurReplace.combined(with: .move(edge: .top)))
                            }
                        }
                    }
                }
                }
                .padding(.horizontal, -6)
                .animation(.smooth(duration: 0.32), value: expanded)
            } accessory: {
                Text("\(snapshot.others.count)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct AccountRow: View {
    let store: Store
    let account: Account
    let isNext: Bool
    let switchBlocked: Bool
    let isOpen: Bool
    let toggle: () -> Void
    @State private var hovering = false
    /// Measured once the button shows; the guess keeps the first fade close.
    @State private var switchWidth: CGFloat = 34
    @Environment(\.now) private var now

    var body: some View {
        HStack(spacing: 8) {
            DisclosureChevron(open: isOpen)
            Text(account.email)
                .font(.callout.weight(.medium))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
                // Switch sits over the end of the address, not in place of the usage: the bars
                // stay readable under the pointer and the row keeps its height
                .mask(alignment: .trailing) { fade }
                .overlay(alignment: .trailing) { switchButton }
            if isNext { Chip("Next", tint: Format.violet) }
            if account.lastResort { Chip("Backup") }
            trailing
                .frame(width: 150, alignment: .trailing)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .help(isOpen ? "" : caption)
        .background(isHovered || isOpen ? AnyShapeStyle(.quaternary) : AnyShapeStyle(.clear), in: .rect(cornerRadius: 14, style: .continuous))
        .contentShape(.rect(cornerRadius: 14, style: .continuous))
        .onTapGesture(perform: toggle)
        .onHover { hovering = $0 }
        .animation(.snappy(duration: 0.18), value: hovering)
        .contextMenu {
            Button("Switch to This Account", systemImage: "arrow.triangle.swap") {
                Task { await store.switchTo(account) }
            }
            .disabled(store.busy != nil || account.status == .needsLogin || switchBlocked)
            Button("Sign In Again", systemImage: "person.badge.key") { Task { await store.login(account) } }
                .disabled(store.busy != nil)
        }
    }

    private var isHovered: Bool {
        hovering || store.previewHoverAccount == account.id
    }

    private var offersSwitch: Bool {
        guard isHovered, !switchBlocked, account.status != .needsLogin else { return false }
        switch store.busy {
        case .switching(let email), .loggingIn(let email): return email != account.email
        case nil: return true
        }
    }

    /// The address fades out where the button covers it; without the button it's fully drawn.
    private var fade: some View {
        HStack(spacing: 0) {
            Rectangle()
            LinearGradient(colors: [.black, .clear], startPoint: .leading, endPoint: .trailing)
                .frame(width: offersSwitch ? 14 : 0)
            Color.clear
                .frame(width: offersSwitch ? switchWidth : 0)
        }
    }

    @ViewBuilder private var switchButton: some View {
        if offersSwitch {
            // icon only: the address is narrow, and it should still say which account this is
            Button { Task { await store.switchTo(account) } } label: {
                Label("Switch", systemImage: "arrow.triangle.swap").labelStyle(.iconOnly)
            }
                .panelButton()
                .controlSize(.small)
                .disabled(store.busy != nil)
                .help("Switch to this account")
                .fixedSize()
                .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { switchWidth = $0 }
                .transition(.opacity.combined(with: .scale(scale: 0.92, anchor: .trailing)))
        }
    }

    /// Everything that doesn't fit in one line goes to the tooltip.
    private var caption: String {
        if account.status == .needsLogin { return "Session expired, sign in again" }
        if account.status == .error || account.session == nil {
            let age = account.dataAge.map { " · data \(Format.age($0)) old" } ?? ""
            return account.note + age
        }
        // the 5h window only starts on first use, so an unused account has no reset
        let session = account.session?.resetsAt.map { "5h \(Format.until($0, now: now))" } ?? "5h unused"
        let weekly = account.weekly?.resetsAt.map { "week \(Format.until($0, now: now)) (\(Format.moment($0, now: now)))" }
        var lines = ["Resets " + [session, weekly].compactMap(\.self).joined(separator: " · ")]
        if let renews = account.renewsAt {
            lines.append("Renews \(Format.day(renews)) · \(Format.inDays(renews))")
        }
        if let status = account.subscriptionStatus, status != "active" {
            lines.append("Subscription \(status), auto-switch skips it")
        }
        if let age = account.dataAge, age > 900 {
            lines.append("Data \(Format.age(age)) old")
        }
        return lines.joined(separator: "\n")
    }

    @ViewBuilder private var trailing: some View {
        switch store.busy {
        case .loggingIn(let email) where email == account.email:
            HStack(spacing: 6) {
                Text("In browser…").font(.caption).foregroundStyle(.orange)
                Button("Cancel") { store.cancelLogin() }
                    .panelButton()
                    .controlSize(.small)
            }
        case .switching(let email) where email == account.email:
            ProgressView().controlSize(.small)
        default:
            if account.status == .needsLogin {
                Button("Sign In") { Task { await store.login(account) } }
                    .panelButton(prominent: true)
                    .controlSize(.small)
                    .disabled(store.busy != nil)
            } else if account.session == nil && account.weekly == nil {
                Text("No data").font(.caption).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 10) {
                    MiniUsage(label: "5h", window: account.session, stale: account.status != .ok)
                    MiniUsage(label: "wk", window: account.weekly, stale: account.status != .ok)
                }
                .transition(.opacity)
            }
        }
    }
}

private struct MiniUsage: View {
    let label: String
    let window: UsageWindow?
    let stale: Bool
    @Environment(\.now) private var now

    var body: some View {
        let used = window?.used
        let tint = stale || (used ?? 0) < 1 ? Color.secondary : Format.tint(used)
        VStack(alignment: .trailing, spacing: 3) {
            HStack(spacing: 3) {
                Text(label).foregroundStyle(.tertiary)
                Text(Format.percent(used))
                    .monospacedDigit()
                    .foregroundStyle(tint)
            }
            .font(.caption2.weight(.semibold))
            UsageBar(fraction: (used ?? 0) / 100, tint: stale ? .secondary : Format.tint(used), height: 4)
                .frame(width: 64)
            // time to the reset; an unused 5h window has none (it starts on first use), but the
            // line keeps its height so the bars of all rows stay aligned
            HStack(spacing: 2) {
                if let reset = window?.resetsAt {
                    Image(systemName: "arrow.clockwise")
                        .imageScale(.small)
                    Text(Format.left(reset, now: now))
                        .monospacedDigit()
                } else {
                    Text(" ")
                }
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
        }
    }
}
