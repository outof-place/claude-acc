import SwiftUI

/// The monthly API credits of the Max plans (credits.py): what is left across every linked
/// Console organization, what expires next (credits don't roll over, so that is what to use
/// first) and how fresh the number is. Shown only once an account is registered.
struct CreditsCard: View {
    let credits: CreditsSummary
    @Environment(\.now) private var now

    var body: some View {
        Card("API Credits", symbol: "creditcard") {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(Format.usd(credits.totalRemainingUsd))
                        .font(.title2.weight(.semibold))
                        .monospacedDigit()
                        .contentTransition(.numericText(value: credits.totalRemainingUsd))
                    Text("left of \(Format.usd(credits.grantedUsd))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                // spending is the point here (unused credit expires), so the bar stays violet
                UsageBar(fraction: credits.spentFraction, tint: Format.violet)
                VStack(alignment: .leading, spacing: 3) {
                    Text(expiry)
                    Text(accounts)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if credits.error > 0 {
                    Label(
                        credits.error == 1 ? "1 organization has no key: claude-acc credits add" : "\(credits.error) organizations have no key: claude-acc credits add",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            }
        } accessory: {
            HStack(spacing: 6) {
                ForEach(credits.byScope.keys.filter { $0 != "own" }.sorted(), id: \.self) { scope in
                    Chip("\(scope.capitalized) \(Format.usd(credits.byScope[scope] ?? 0))")
                        .help("Credit of the \(scope) scope: pays only for \(scope) work")
                }
            }
        }
        .help("Console is the truth: claude-acc credits balance <email> --remaining-usd N records a reading")
    }

    private var expiry: String {
        guard let at = credits.nextExpiryAt, let usd = credits.nextExpiryUsd else {
            return credits.linked == 0 ? "No organization linked yet" : "Nothing left to expire this cycle"
        }
        return "\(Format.usd(usd)) expires \(Format.day(at)), \(Format.inDays(at, now: now))"
    }

    private var accounts: String {
        var parts = [credits.linked == 1 ? "1 organization" : "\(credits.linked) organizations"]
        if credits.pending > 0 { parts.append("\(credits.pending) waiting for the button") }
        if let checked = credits.checkedAt {
            parts.append("Console read \(Format.ago(checked, now: now))")
        } else if credits.linked > 0 {
            parts.append("estimated, no Console reading")
        }
        return parts.joined(separator: " · ")
    }
}
