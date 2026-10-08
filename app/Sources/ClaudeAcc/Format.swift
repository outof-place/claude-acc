import SwiftUI

enum Format {
    private static let english = Locale(identifier: "en_US")

    private static let clock = Date.VerbatimFormatStyle(
        format: "\(hour: .twoDigits(clock: .twentyFourHour, hourCycle: .zeroBased)):\(minute: .twoDigits)",
        locale: english, timeZone: .current, calendar: .current)

    private static let weekdayClock = Date.VerbatimFormatStyle(
        format: "\(weekday: .abbreviated) \(hour: .twoDigits(clock: .twentyFourHour, hourCycle: .zeroBased)):\(minute: .twoDigits)",
        locale: english, timeZone: .current, calendar: .current)

    private static let monthDay = Date.VerbatimFormatStyle(
        format: "\(month: .abbreviated) \(day: .defaultDigits)",
        locale: english, timeZone: .current, calendar: .current)

    /// "18:40", or "Tue 07:00" when it isn't the same day as `now`.
    static func moment(_ epoch: Double, now: Date = .now) -> String {
        let date = Date(timeIntervalSince1970: epoch)
        return Calendar.current.isDate(date, inSameDayAs: now) ? date.formatted(clock) : date.formatted(weekdayClock)
    }

    /// "Oct 15".
    static func day(_ epoch: Double) -> String {
        Date(timeIntervalSince1970: epoch).formatted(monthDay)
    }

    private static let fullStamp = Date.VerbatimFormatStyle(
        format: "\(weekday: .abbreviated) \(month: .abbreviated) \(day: .defaultDigits), \(hour: .twoDigits(clock: .twentyFourHour, hourCycle: .zeroBased)):\(minute: .twoDigits)",
        locale: english, timeZone: .current, calendar: .current)

    private static let fullDay = Date.VerbatimFormatStyle(
        format: "\(weekday: .abbreviated) \(month: .abbreviated) \(day: .defaultDigits), \(year: .defaultDigits)",
        locale: english, timeZone: .current, calendar: .current)

    /// Exact moment for the details view: "today 18:40", "tomorrow 07:00", "Sat Oct 11, 07:00".
    static func stamp(_ epoch: Double, now: Date) -> String {
        let date = Date(timeIntervalSince1970: epoch)
        let calendar = Calendar.current
        if calendar.isDate(date, inSameDayAs: now) { return "today \(date.formatted(clock))" }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: now), calendar.isDate(date, inSameDayAs: tomorrow) {
            return "tomorrow \(date.formatted(clock))"
        }
        return date.formatted(fullStamp)
    }

    /// "Sat Oct 28, 2026".
    static func fullDate(_ epoch: Double) -> String {
        Date(timeIntervalSince1970: epoch).formatted(fullDay)
    }

    /// Countdown to the minute: "5d 2h 14m", "2h 06m", "14m".
    static func countdown(_ epoch: Double, now: Date) -> String {
        let minutes = max(Int((epoch - now.timeIntervalSince1970) / 60), 0)
        let (d, h, m) = (minutes / 1440, minutes % 1440 / 60, minutes % 60)
        if d > 0 { return "\(d)d \(h)h \(String(format: "%02d", m))m" }
        if h > 0 { return "\(h)h \(String(format: "%02d", m))m" }
        return "\(m)m"
    }

    /// "2025-03-28" from the profile API → "Mar 28, 2025".
    static func isoDay(_ text: String) -> String {
        guard let date = try? Date(text, strategy: .iso8601.year().month().day()) else { return text }
        return date.formatted(.dateTime.month(.abbreviated).day().year().locale(english))
    }

    /// "in 1d 12h", "in 2h 25m", "in 40 min".
    static func until(_ epoch: Double, now: Date) -> String {
        let minutes = max(Int((epoch - now.timeIntervalSince1970) / 60), 0)
        let (h, m) = (minutes / 60, minutes % 60)
        if h >= 24 { return "in \(h / 24)d \(h % 24)h" }
        return h > 0 ? "in \(h)h \(m)m" : "in \(m) min"
    }

    /// "3d 4h", "2h 15m", "40m", "now": time left, short enough for a line under a usage bar.
    static func left(_ epoch: Double, now: Date) -> String {
        let minutes = Int((epoch - now.timeIntervalSince1970) / 60)
        if minutes <= 0 { return "now" }
        let (h, m) = (minutes / 60, minutes % 60)
        if h >= 24 { return "\(h / 24)d \(h % 24)h" }
        return h > 0 ? "\(h)h \(m)m" : "\(m)m"
    }

    /// "just now", "35s ago", "12 min ago", "3h ago", "2d ago".
    static func ago(_ epoch: Double?, now: Date) -> String {
        guard let epoch else { return "never" }
        let seconds = Int(now.timeIntervalSince1970 - epoch)
        switch seconds {
        case ..<10: return "just now"
        case ..<60: return "\(seconds)s ago"
        case ..<3600: return "\(seconds / 60) min ago"
        case ..<86_400: return "\(seconds / 3600)h ago"
        default: return "\(seconds / 86_400)d ago"
        }
    }

    /// Days to a date that has no time of day: "today", "tomorrow", "in 19 days".
    static func inDays(_ epoch: Double, now: Date = .now) -> String {
        let calendar = Calendar.current
        let days = calendar.dateComponents(
            [.day], from: calendar.startOfDay(for: now),
            to: calendar.startOfDay(for: Date(timeIntervalSince1970: epoch))).day ?? 0
        return switch days {
        case ..<1: "today"
        case 1: "tomorrow"
        default: "in \(days) days"
        }
    }

    /// Data age without "ago": "12 min", "3h 5m", "1d 14h".
    static func age(_ seconds: Int) -> String {
        switch seconds {
        case ..<60: "\(seconds)s"
        case ..<3600: "\(seconds / 60) min"
        case ..<86_400: "\(seconds / 3600)h \(seconds % 3600 / 60)m"
        default: "\(seconds / 86_400)d \(seconds % 86_400 / 3600)h"
        }
    }

    /// "$2,340" from a hundred up, "$12.50" below: cents matter only when little is left.
    static func usd(_ value: Double) -> String {
        value.formatted(.currency(code: "USD").precision(.fractionLength(value >= 100 ? 0 : 2)).locale(english))
    }

    static func percent(_ value: Double?) -> String {
        value.map { "\(Int($0.rounded()))%" } ?? "-"
    }

    /// "8.2 GB", "412 MB": powers of 1024 and one decimal from a gigabyte up, like janitor.py,
    /// devguard.py and `df -h`, so the panel and the terminal show the same numbers.
    static func bytes(_ value: Double) -> String {
        let units: [(size: Double, name: String, digits: Int)] = [
            (gigabyte * 1024, "TB", 1), (gigabyte, "GB", 1), (1_048_576, "MB", 0), (1024, "KB", 0),
        ]
        let value = max(value, 0)
        guard let unit = units.first(where: { value >= $0.size }) else { return "\(Int(value)) B" }
        let number = (value / unit.size).formatted(.number.precision(.fractionLength(unit.digits)).locale(english))
        return "\(number) \(unit.name)"
    }

    /// "3,480".
    static func rpm(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0)).locale(english))
    }

    static let violet = Color(red: 0.58, green: 0.49, blue: 1.0)
    static let nsViolet = NSColor(srgbRed: 0.58, green: 0.49, blue: 1.0, alpha: 1)

    /// Usage colour: violet, then a warning, red right before the wall.
    static func tint(_ used: Double?) -> Color {
        guard let used else { return .secondary }
        return switch used {
        case ..<75: violet
        case ..<90: .orange
        default: .red
        }
    }

    static func nsTint(_ used: Double?) -> NSColor {
        guard let used else { return .secondaryLabelColor }
        return switch used {
        case ..<75: nsViolet
        case ..<90: .systemOrange
        default: .systemRed
        }
    }

    static func pressure(_ level: Int) -> (text: String, color: Color) {
        switch level {
        case 2: ("Critical", .red)
        case 1: ("Elevated", .orange)
        default: ("Normal", .green)
        }
    }

    /// Why the guard acts, from the reason code and its numbers.
    static func reason(_ code: String?, _ data: GuardReason?) -> String {
        switch code {
        case "bloated": "grew to \(bytes(data?.size ?? 0))"
        case "bloated_unmanaged": "at \(bytes(data?.size ?? 0)), no terminal to restart it in"
        case "duplicate": data?.keep.map { "duplicate of :\($0)" } ?? "duplicate"
        case "orphan": "its agent or terminal is gone"
        case "idle": "idle for \(data?.minutes ?? 0) min"
        case "budget": "over the \(bytes(data?.budget ?? 0)) budget"
        case "pressure": data?.level == 2 ? "memory is critical" : "memory is tight"
        case "loop": "restarted \(data?.restarts ?? 0)× this hour, HMR loop"
        case "loop_watched": "keeps regrowing, \(data?.restarts ?? 0) restarts this hour"
        case "manual": "by hand"
        default: ""
        }
    }
}
