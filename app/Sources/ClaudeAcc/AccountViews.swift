import SwiftUI

// MARK: - Active account

struct ActiveAccountCard: View {
    let store: Store

    var body: some View {
        Card("Claude Code", symbol: "sparkle") {
            if let snapshot = store.snapshot {
                if let active = snapshot.active {
                    ActiveAccount(store: store, snapshot: snapshot, account: active)
                } else {
                    Text("Claude Code is signed in to an account outside Orca. Auto-switch leaves it alone until you switch to one from the list.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
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

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
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
                    Text("Resets \(Format.until(reset, now: now))")
                        .help("Resets \(Format.moment(reset, now: now))")
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
                    .glassButton()
            } else {
                Label("Session expired", systemImage: "person.crop.circle.badge.exclamationmark")
                    .foregroundStyle(.orange)
                Spacer()
                Button("Sign In") { Task { await store.login(account) } }
                    .glassButton(prominent: true)
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

    var body: some View {
        if let snapshot = store.snapshot, !snapshot.others.isEmpty {
            Card("Accounts", symbol: "person.2") {
                VStack(spacing: 2) {
                    ForEach(snapshot.others) { account in
                        AccountRow(
                            store: store, account: account,
                            isNext: account.id == snapshot.next?.id,
                            switchBlocked: snapshot.orcaSelected != nil)
                    }
                }
                .padding(.horizontal, -6)
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
    @State private var hovering = false
    @Environment(\.now) private var now

    var body: some View {
        HStack(spacing: 8) {
            Text(account.email)
                .font(.callout.weight(.medium))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            if isNext { Chip("Next", tint: Format.violet) }
            if account.lastResort { Chip("Backup") }
            trailing
                .frame(width: 150, alignment: .trailing)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .help(caption)
        .background(hovering ? AnyShapeStyle(.quaternary) : AnyShapeStyle(.clear), in: .rect(cornerRadius: 14, style: .continuous))
        .contentShape(.rect(cornerRadius: 14, style: .continuous))
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
                    .glassButton()
                    .controlSize(.small)
            }
        case .switching(let email) where email == account.email:
            ProgressView().controlSize(.small)
        default:
            if account.status == .needsLogin {
                Button("Sign In") { Task { await store.login(account) } }
                    .glassButton(prominent: true)
                    .controlSize(.small)
                    .disabled(store.busy != nil)
            } else if hovering && !switchBlocked {
                Button("Switch", systemImage: "arrow.triangle.swap") { Task { await store.switchTo(account) } }
                    .glassButton()
                    .controlSize(.small)
                    .disabled(store.busy != nil)
                    .transition(.opacity.combined(with: .scale(scale: 0.92)))
            } else if account.session == nil && account.weekly == nil {
                Text("No data").font(.caption).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 10) {
                    MiniUsage(label: "5h", used: account.session?.used, stale: account.status != .ok)
                    MiniUsage(label: "wk", used: account.weekly?.used, stale: account.status != .ok)
                }
                .transition(.opacity)
            }
        }
    }
}

private struct MiniUsage: View {
    let label: String
    let used: Double?
    let stale: Bool

    var body: some View {
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
        }
    }
}
