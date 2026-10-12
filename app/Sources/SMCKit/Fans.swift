import Foundation

public struct FanReading: Codable, Equatable {
    public let index: Int
    public let rpm: Double
    public let min: Double
    public let max: Double
    public let target: Double
    public let manual: Bool
}

public struct Reading: Codable {
    public var at = Date.now.timeIntervalSince1970
    public var fans: [FanReading] = []
    /// Hottest CPU and GPU die sensors, °C.
    public var cpu: Double?
    public var gpu: Double?
    /// Hottest sensor per part, °C: pcores, ecores, gpu, ssd, battery.
    public var sensors: [String: Double] = [:]
    /// [time, cpu, gpu, average rpm] every 5 seconds, 20 minutes back (written by the daemon;
    /// temperatures to 0.1 °C, rpm and time whole).
    public var history: [[Double]]?
    /// What the daemon holds the fans at: "auto" or "fixed", and the share of the range.
    public var mode: String?
    public var percent: Int?
    /// Set while a chip runs hot and a fixed setting was overridden to full speed.
    public var boosting: Bool?
    /// Another app (Mole, Macs Fan Control…) set the fans after we did; we don't fight it.
    public var conflict: Bool?
    public var error: String?
    /// The daemon turned `SleepDisabled` on for Stay Awake with the lid closed.
    public var lidHeld: Bool?

    public init() {}
}

/// Fans on Apple Silicon: `FNum` fans, each with `F<n>Ac` (actual rpm), `F<n>Mn`/`F<n>Mx`
/// (range), `F<n>Tg` (target) and `F<n>Md` (`F<n>md` before M4; 0 = macOS decides, 1 = target is held).
/// `Ftst` = 1 unlocks manual control on the M-series SMC.
public final class Fans {
    public enum Mode: Equatable, CustomStringConvertible {
        case auto
        case fixed(percent: Int)

        public var description: String {
            switch self {
            case .auto: "auto"
            case .fixed(let percent): "\(percent)%"
            }
        }
    }

    public let smc: SMC
    private lazy var sensors: [String: [String]] = discoverSensors()
    /// M4 calls the mode key F<n>Md, earlier M-series F<n>md.
    private lazy var upperMode: Bool = (try? smc.info("F0Md")) != nil

    private func modeKey(_ fan: Int) -> String { upperMode ? "F\(fan)Md" : "F\(fan)md" }

    public init(smc: SMC) { self.smc = smc }

    /// FNum doesn't change while the Mac runs: read until it answers, then kept.
    private var fanCount: Int?

    public var count: Int {
        if let fanCount { return fanCount }
        guard let value = try? smc.number("FNum") else { return 0 }
        fanCount = Int(value)
        return Int(value)
    }

    /// `sensors: false` reads only the fans: a dozen SMC calls instead of ~170 on an M4 Max.
    public func reading(sensors withSensors: Bool = true) -> Reading {
        var reading = Reading()
        for i in 0..<count {
            reading.fans.append(FanReading(
                index: i,
                rpm: (try? smc.number("F\(i)Ac")) ?? 0,
                min: (try? smc.number("F\(i)Mn")) ?? 0,
                max: (try? smc.number("F\(i)Mx")) ?? 0,
                target: (try? smc.number("F\(i)Tg")) ?? 0,
                manual: ((try? smc.number(modeKey(i))) ?? 0) >= 1))
        }
        guard withSensors else { return reading }
        for (part, keys) in sensors {
            if let value = hottest(keys) { reading.sensors[part] = value }
        }
        reading.cpu = [reading.sensors["pcores"], reading.sensors["ecores"]].compactMap(\.self).max()
        reading.gpu = reading.sensors["gpu"]
        return reading
    }

    public func apply(_ mode: Mode) throws {
        switch mode {
        case .auto:
            for i in 0..<count { try smc.setNumber(modeKey(i), 0) }
            try? smc.setNumber("Ftst", 0)
        case .fixed(let percent):
            try? smc.setNumber("Ftst", 1)
            for i in 0..<count {
                let low = try smc.number("F\(i)Mn")
                let high = try smc.number("F\(i)Mx")
                try smc.setNumber(modeKey(i), 1)
                try smc.setNumber("F\(i)Tg", low + (high - low) * Double(percent) / 100)
            }
        }
    }

    /// Is the hardware still where we put it? The SMC drops manual mode after sleep.
    public func holds(_ mode: Mode, _ reading: Reading) -> Bool {
        switch mode {
        case .auto:
            return reading.fans.allSatisfy { !$0.manual }
        case .fixed(let percent):
            // the SMC trims a held target by a few percent on its own; another app moves it far
            return reading.fans.allSatisfy { fan in
                let range = fan.max - fan.min
                let wanted = fan.min + range * Double(percent) / 100
                return fan.manual && abs(fan.target - wanted) <= max(range * 0.15, 150)
            }
        }
    }

    // MARK: Sensors

    /// Sensor families on M-series: `Tp*` performance cores, `Te*` efficiency cores, `Tg*` GPU,
    /// `TH*` the SSD, `TB*` the battery. Only keys that read a plausible temperature count:
    /// some are placeholders stuck near 5 °C. (`Tf*6` on M4 read 95-105 °C while every core
    /// around them reads 50, so they are limits, not dies, and stay out.)
    private func discoverSensors() -> [String: [String]] {
        let keys = (try? smc.allKeys()) ?? []
        func plausible(_ key: String) -> Bool {
            guard let info = try? smc.info(key), info.type == "flt ", let value = try? smc.number(key) else { return false }
            return (10...130).contains(value)
        }
        let families = ["pcores": "Tp", "ecores": "Te", "gpu": "Tg", "ssd": "TH", "battery": "TB"]
        return families.mapValues { prefix in keys.filter { $0.hasPrefix(prefix) && plausible($0) } }
    }

    private func hottest(_ keys: [String]) -> Double? {
        keys.compactMap { try? smc.number($0) }.filter { (10...130).contains($0) }.max()
    }
}
