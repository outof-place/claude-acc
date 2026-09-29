import SwiftUI

enum Format {
    private static let time: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "pl_PL")
        f.dateFormat = "HH:mm"
        return f
    }()

    private static let dayTime: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "pl_PL")
        f.dateFormat = "EEE HH:mm"
        return f
    }()

    /// Godzina, a gdy to nie dziś, także dzień tygodnia ("wt 07:00").
    static func moment(_ epoch: Double) -> String {
        let date = Date(timeIntervalSince1970: epoch)
        return Calendar.current.isDateInToday(date) ? time.string(from: date) : dayTime.string(from: date)
    }

    /// Ile zostało do chwili: "za 1d 12h", "za 2h 25min", "za 40 min".
    static func until(_ epoch: Double, now: Date = Date()) -> String {
        let minutes = max(Int((epoch - now.timeIntervalSince1970) / 60), 0)
        let (h, m) = (minutes / 60, minutes % 60)
        if h >= 24 { return "za \(h / 24)d \(h % 24)h" }
        return h > 0 ? "za \(h)h \(m)min" : "za \(m) min"
    }

    static func percent(_ value: Double?) -> String {
        guard let value else { return "-" }
        return "\(Int(value.rounded()))%"
    }

    private static let day: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "pl_PL")
        f.dateFormat = "d.MM"
        return f
    }()

    /// Dzień bez godziny: "15.10".
    static func day(_ epoch: Double) -> String {
        day.string(from: Date(timeIntervalSince1970: epoch))
    }

    /// Dni do daty bez godziny (odnowienie zna tylko dzień): "dziś", "jutro", "za 19 dni".
    static func inDays(_ epoch: Double) -> String {
        let calendar = Calendar.current
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: Date()),
                                           to: calendar.startOfDay(for: Date(timeIntervalSince1970: epoch))).day ?? 0
        switch days {
        case ..<1: return "dziś"
        case 1: return "jutro"
        default: return "za \(days) dni"
        }
    }

    /// Wiek danych bez słowa "temu": "12 min", "3h", "1d 14h".
    static func age(_ seconds: Int) -> String {
        let hours = seconds / 3600
        if hours >= 24 { return "\(hours / 24)d \(hours % 24)h" }
        return seconds < 3600 ? "\(seconds / 60) min" : "\(hours)h"
    }

    static func ago(_ epoch: Double?, now: Date) -> String {
        guard let epoch else { return "nigdy" }
        let seconds = Int(now.timeIntervalSince1970 - epoch)
        switch seconds {
        case ..<10: return "przed chwilą"
        case ..<60: return "\(seconds) s temu"
        case ..<3600: return "\(seconds / 60) min temu"
        default: return "\(seconds / 3600) godz. temu"
        }
    }

    static let violet = NSColor(srgbRed: 0.58, green: 0.49, blue: 1.0, alpha: 1)

    /// Kolor zużycia: fiolet, potem ostrzeżenie, a tuż przed ścianą czerwień.
    static func tint(_ used: Double?) -> Color {
        guard let used else { return .secondary }
        switch used {
        case ..<75: return Color(nsColor: violet)
        case ..<90: return .orange
        default: return .red
        }
    }

    static func nsTint(_ used: Double?) -> NSColor {
        guard let used else { return .secondaryLabelColor }
        switch used {
        case ..<75: return violet
        case ..<90: return .systemOrange
        default: return .systemRed
        }
    }
}
