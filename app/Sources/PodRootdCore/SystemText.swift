import PodRootdProtocol

/// What the tools pod-rootd has to run print, read back into values.
public enum SystemText {
    /// "Battery Power:" and "AC Power:" sections, each with a " powermode N" line.
    public static func powerModes(_ text: String) -> [PowerSource: PowerMode] {
        var modes: [PowerSource: PowerMode] = [:]
        var section: PowerSource?
        for line in text.split(separator: "\n") {
            if line.hasPrefix("Battery Power") { section = .battery } else if line.hasPrefix("AC Power") { section = .ac }
            let words = line.split(separator: " ")
            if let section, words.count == 2, words[0] == "powermode", let raw = Int64(words[1]),
               let mode = PowerMode(rawValue: raw) {
                modes[section] = mode
            }
        }
        return modes
    }

    /// `ifconfig -v` says "uplink rate: 25.10 Mbps [eff] / 27.00 Mbps [tbr] / 1.00 Gbps [max]";
    /// the number before "[tbr]" in kb/s, nil without a limit.
    public static func tbr(_ text: String) -> Int64? {
        guard let line = text.split(separator: "\n").first(where: { $0.contains("uplink rate:") && $0.contains("[tbr]") })
        else { return nil }
        let words = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
        guard let at = words.firstIndex(of: "[tbr]"), at >= 2, let number = Double(words[at - 2]) else { return nil }
        let scale: Double = switch words[at - 1] {
        case "bps": 0.001
        case "Kbps": 1
        case "Mbps": 1000
        case "Gbps": 1_000_000
        default: 0
        }
        let kbps = Int64((number * scale).rounded())
        return kbps > 0 ? kbps : nil
    }
}
