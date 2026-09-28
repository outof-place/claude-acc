import SwiftUI

struct PanelView: View {
    let store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let snapshot = store.snapshot {
                if let orca = snapshot.orcaSelected {
                    OrcaWarning(email: orca)
                        .padding([.horizontal, .top], 14)
                }
                ActiveSection(store: store, snapshot: snapshot)
                    .padding(14)
                Divider()
                OthersSection(store: store, snapshot: snapshot)
                    .padding(.vertical, 8)
                Divider()
            } else {
                EmptyState(store: store)
                    .padding(14)
                Divider()
            }
            Footer(store: store)
                .padding(14)
        }
        .frame(width: 350)
        .onAppear { store.refreshIfStale() }
    }
}

// MARK: aktywne konto

private struct ActiveSection: View {
    let store: Store
    let snapshot: Snapshot

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("Claude Code").font(.headline)
                Spacer()
                if let tier = snapshot.active?.tier, !tier.isEmpty {
                    Tag(text: tier)
                }
            }
            if let active = snapshot.active {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(active.email)
                        Spacer()
                        SubscriptionText(account: active)
                    }
                    .font(.subheadline).foregroundStyle(.secondary)
                    if active.mislabeled, let real = active.realEmail {
                        Label("Ten wpis Orca trzyma konto \(real)", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                if active.status == .needsLogin {
                    LoginPrompt(store: store, account: active)
                } else {
                    UsageBlock(title: "Sesja 5h", window: active.session, forecast: snapshot.forecast?.session,
                               threshold: snapshot.thresholds.sessionLeft, stale: active.status != .ok)
                    UsageBlock(title: "Tydzień", window: active.weekly, forecast: snapshot.forecast?.weekly,
                               threshold: snapshot.thresholds.weeklyLeft, stale: active.status != .ok)
                    if active.status == .error {
                        Text(active.note).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Label("W Claude Code jest zalogowane konto spoza Orca. Automat go nie rusza, dopóki nie przełączysz na konto z listy.",
                      systemImage: "questionmark.circle")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct UsageBlock: View {
    let title: String
    let window: UsageWindow?
    let forecast: WindowForecast?
    let threshold: Double
    let stale: Bool

    var body: some View {
        let used = window?.used
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(title).font(.callout)
                Spacer()
                Text(Format.percent(used)).font(.callout.weight(.semibold)).monospacedDigit()
            }
            UsageBar(fraction: (used ?? 0) / 100, tint: stale ? .secondary : Format.tint(used),
                     marker: 1 - threshold / 100)
            HStack {
                forecastText
                Spacer()
                if let reset = window?.resetsAt {
                    Text("Reset \(Format.moment(reset)) · \(Format.until(reset))")
                } else {
                    Text(window == nil ? "Brak danych" : "Okno jeszcze nie ruszyło")
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var forecastText: some View {
        if let switchAt = forecast?.switchAt {
            Text("Przełączy ok. \(Format.moment(switchAt))").foregroundStyle(.orange)
        } else if let forecast {
            Text("W normie · ~\(Int(forecast.atReset.rounded()))% przy resecie")
        } else {
            Text(" ")
        }
    }
}

// MARK: pozostałe konta

private struct OthersSection: View {
    let store: Store
    let snapshot: Snapshot

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Pozostałe konta")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 14)
                .padding(.bottom, 4)
            ForEach(snapshot.others) { account in
                AccountRow(store: store, account: account, isNext: account.id == snapshot.next?.id,
                           switchBlocked: snapshot.orcaSelected != nil)
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

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text(account.email).font(.callout).lineLimit(1).truncationMode(.middle)
                    if isNext { Tag(text: "następne", tint: Color(nsColor: Format.violet)) }
                    if account.lastResort { Tag(text: "firmowe") }
                }
                details
            }
            Spacer(minLength: 0)
            trailing
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(hovering ? Color.primary.opacity(0.06) : .clear)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Przełącz na to konto") { Task { await store.switchTo(account) } }
                .disabled(store.busy != nil || account.status == .needsLogin || switchBlocked)
            Button("Zaloguj ponownie") { Task { await store.login(account) } }
                .disabled(store.busy != nil)
        }
    }

    @ViewBuilder private var details: some View {
        if store.busy == .loggingIn(account.email) {
            Text("Dokończ logowanie w przeglądarce").font(.caption).foregroundStyle(.orange)
        } else if account.status == .needsLogin {
            Text("Sesja wygasła, zaloguj ponownie").font(.caption).foregroundStyle(.orange)
        } else if account.session == nil {
            Text(account.note).font(.caption).foregroundStyle(.secondary).lineLimit(2)
        } else {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 10) {
                    MiniUsage(label: "5h", used: account.session?.used, stale: account.status != .ok)
                    MiniUsage(label: "tydz.", used: account.weekly?.used, stale: account.status != .ok)
                }
                Text(caption).font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                SubscriptionText(account: account).font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }

    private var caption: String {
        if account.status == .error {
            let age = account.dataAge.map { " (dane sprzed \($0 / 60) min)" } ?? ""
            return account.note + age
        }
        // okno 5h rusza dopiero przy pierwszym użyciu, więc nieużywane konto nie ma resetu
        let session = account.session?.resetsAt.map { "5h \(Format.until($0))" } ?? "5h nieużywane"
        let weekly = account.weekly?.resetsAt.map { "tydzień \(Format.until($0)) (\(Format.moment($0)))" }
        var text = "reset: " + [session, weekly].compactMap { $0 }.joined(separator: " · ")
        if let age = account.dataAge, age > 900 {
            text += " · dane sprzed \(Format.age(age))"
        }
        return text
    }

    @ViewBuilder private var trailing: some View {
        switch store.busy {
        case .loggingIn(let email) where email == account.email:
            Button("Anuluj") { store.cancelLogin() }
                .controlSize(.small)
        case .switching(let email) where email == account.email:
            ProgressView().controlSize(.small)
        default:
            if account.status == .needsLogin {
                Button("Zaloguj") { Task { await store.login(account) } }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .disabled(store.busy != nil)
            } else if hovering && !switchBlocked {
                Button("Przełącz") { Task { await store.switchTo(account) } }
                    .controlSize(.small)
                    .disabled(store.busy != nil)
            }
        }
    }
}

/// Najbliższe odnowienie subskrypcji albo jej stan, gdy przestała być aktywna.
private struct SubscriptionText: View {
    let account: Account

    var body: some View {
        if let status = account.subscriptionStatus, status != "active" {
            Text("subskrypcja: \(status), automat pomija").foregroundStyle(.orange)
        } else if let renews = account.renewsAt {
            Text("odnowienie \(Format.day(renews)) · \(Format.inDays(renews))")
        }
    }
}

private struct MiniUsage: View {
    let label: String
    let used: Double?
    let stale: Bool

    var body: some View {
        HStack(spacing: 5) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
            UsageBar(fraction: (used ?? 0) / 100, tint: stale ? .secondary : Format.tint(used), height: 4)
                .frame(width: 58)
            Text(Format.percent(used)).font(.caption2).monospacedDigit().foregroundStyle(.secondary)
                .frame(width: 30, alignment: .trailing)
        }
    }
}

private struct LoginPrompt: View {
    let store: Store
    let account: Account

    var body: some View {
        HStack {
            if store.busy == .loggingIn(account.email) {
                Text("Dokończ logowanie w przeglądarce").font(.callout).foregroundStyle(.orange)
                Spacer()
                Button("Anuluj") { store.cancelLogin() }
            } else {
                Text("Sesja tego konta wygasła").font(.callout).foregroundStyle(.orange)
                Spacer()
                Button("Zaloguj") { Task { await store.login(account) } }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.busy != nil)
            }
        }
    }
}

// MARK: stopka

private struct Footer: View {
    let store: Store

    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { context in
            VStack(alignment: .leading, spacing: 8) {
                if let notice = store.notice {
                    HStack(alignment: .top) {
                        Text(notice.text)
                            .foregroundStyle(notice.isError ? .red : .primary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        Button { store.notice = nil } label: { Image(systemName: "xmark") }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
                if let problem = store.problem {
                    Text(problem).font(.caption).foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let snapshot = store.snapshot {
                    automat(snapshot, now: context.date)
                }
                HStack {
                    Text("Zaktualizowano \(Format.ago(store.snapshot?.generatedAt, now: context.date))")
                    Spacer()
                    Button {
                        Task { await store.refresh() }
                    } label: {
                        if store.refreshing {
                            ProgressView().controlSize(.mini)
                        } else {
                            Image(systemName: "arrow.clockwise")
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(store.refreshing)
                    .help("Odśwież")
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                Divider()

                Toggle("Uruchamiaj przy logowaniu", isOn: Binding(
                    get: { store.launchAtLogin },
                    set: { store.setLaunchAtLogin($0) }))
                    .toggleStyle(.checkbox)
                    .font(.callout)
                HStack {
                    Button("Historia przełączeń") {
                        NSWorkspace.shared.open(URL(fileURLWithPath: CLI.logFile))
                    }
                    Spacer()
                    Button("Zakończ") { NSApp.terminate(nil) }
                        .keyboardShortcut("q")
                }
                .buttonStyle(.plain)
                .font(.callout)
            }
        }
    }

    @ViewBuilder private func automat(_ snapshot: Snapshot, now: Date) -> some View {
        let t = snapshot.thresholds
        let tickAge = snapshot.lastTick.map { now.timeIntervalSince1970 - $0 }
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Circle()
                    .fill(tickAge.map { $0 < 600 } == true && snapshot.orcaSelected == nil ? Color.green : Color.orange)
                    .frame(width: 7, height: 7)
                if snapshot.orcaSelected != nil {
                    Text("Automat wstrzymany, konto wybrane w Orca")
                } else if let tickAge, tickAge < 600 {
                    Text("Automat sprawdzał \(Format.ago(snapshot.lastTick, now: now))")
                } else if snapshot.lastTick == nil {
                    Text("Automat jeszcze się nie odezwał")
                } else {
                    Text("Automat milczy od \(Format.ago(snapshot.lastTick, now: now).replacingOccurrences(of: " temu", with: ""))")
                }
            }
            Text("Przełącza, gdy zostaje \(Int(t.sessionLeft))% sesji albo \(Int(t.weeklyLeft))% tygodnia")
                .foregroundStyle(.secondary)
            if let until = snapshot.apiBackoffUntil, until > now.timeIntervalSince1970 {
                Text("API limitów chwilowo odmawia (429), odczyty wracają o \(Format.moment(until))")
                    .foregroundStyle(.orange)
            }
        }
        .font(.caption)
    }
}

// MARK: drobne elementy

/// Orca w trybie kont zarządzanych: cofa przełączenia i sama odświeża tokeny,
/// więc automat stoi, żeby nie ścigać się z nią o refresh token.
private struct OrcaWarning: View {
    let email: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("W Orca wybrane jest konto \(email)", systemImage: "exclamationmark.triangle.fill")
                .font(.callout.weight(.semibold))
            Text("Orca cofa przełączenia i sama odświeża tokeny, a dwóch odświeżających wylogowuje konta. Automat i przełączanie stoją. W Orca, w menu kont Claude, wybierz System default.")
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
        }
        .foregroundStyle(.orange)
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct EmptyState: View {
    let store: Store

    var body: some View {
        HStack(spacing: 8) {
            if store.problem == nil {
                ProgressView().controlSize(.small)
                Text("Czytam limity kont…").foregroundStyle(.secondary)
            } else {
                Text("Nie mam jeszcze danych o kontach").foregroundStyle(.secondary)
            }
        }
        .font(.callout)
    }
}

struct UsageBar: View {
    let fraction: Double
    let tint: Color
    var marker: Double? = nil
    var height: CGFloat = 6

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                if fraction > 0 {
                    Capsule().fill(tint)
                        .frame(width: max(height, geo.size.width * min(fraction, 1)))
                }
                if let marker {
                    // kreska w miejscu, w którym automat przełącza konto
                    Rectangle().fill(.secondary)
                        .frame(width: 1.5, height: height + 4)
                        .offset(x: geo.size.width * marker - 0.75)
                }
            }
        }
        .frame(height: height)
    }
}

private struct Tag: View {
    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(tint.opacity(0.15), in: Capsule())
    }
}
