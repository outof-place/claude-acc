import Foundation

/// Odpowiedź `claude-acc status --json`. Całą logikę kont trzyma skrypt,
/// aplikacja tylko ją pokazuje i woła jego komendy.
struct Snapshot: Decodable {
    let generatedAt: Double
    let activeEmail: String?
    let foreignRuntime: Bool
    let thresholds: Thresholds
    let forecast: Forecast?
    let apiBackoffUntil: Double?
    let lastTick: Double?
    let switchedAt: Double?
    let accounts: [Account]

    var active: Account? { accounts.first { $0.active } }
    var others: [Account] { accounts.filter { !$0.active } }
    /// Konto, na które automat przełączy jako następne.
    var next: Account? { others.filter { $0.queue != nil }.min { $0.queue! < $1.queue! } }
    var anyNeedsLogin: Bool { accounts.contains { $0.status == .needsLogin } }
}

struct Thresholds: Decodable {
    let sessionLeft: Double
    let weeklyLeft: Double
}

struct Forecast: Decodable {
    let session: WindowForecast?
    let weekly: WindowForecast?
}

struct WindowForecast: Decodable {
    let rate: Double
    let atReset: Double
    let switchAt: Double?
}

struct UsageWindow: Decodable {
    let used: Double?
    let resetsAt: Double?
}

struct Account: Decodable, Identifiable {
    enum Status: String, Decodable {
        case ok
        case needsLogin = "needs_login"
        case error
    }

    let id: String
    let email: String
    let realEmail: String?
    let tier: String
    let active: Bool
    let lastResort: Bool
    let status: Status
    let note: String
    let usable: Bool
    let queue: Int?
    let session: UsageWindow?
    let weekly: UsageWindow?
    let dataAge: Int?
    /// Miesięczna rocznica startu subskrypcji: API nie podaje daty z rachunku.
    let renewsAt: Double?
    let subscriptionStatus: String?

    /// Wpis w Orca potrafi trzymać inne konto niż głosi jego etykieta.
    var mislabeled: Bool { realEmail != nil && realEmail?.lowercased() != email.lowercased() }
    /// Najbardziej zużyte okno: to ono pierwsze zatrzyma pracę.
    var worstUsed: Double? {
        [session?.used, weekly?.used].compactMap { $0 }.max()
    }
}
