import Foundation

/// Root LaunchDaemon loop. The menu bar app writes the wanted mode to a JSON file in the user's
/// folder; this follows it every 2 seconds and writes readings back for the panel.
///
/// Rules, in this order:
/// - no config file means hands off: installing the daemon changes nothing until a mode is picked;
/// - it writes the SMC only when the picked mode changes, or when a fixed setting was lost to
///   sleep (the fans went back to auto). If another app sets the fans after us, that's a conflict:
///   it is reported, not fought, until the user picks a mode again;
/// - a fixed setting never lets a chip cook: at 95 °C on the hottest CPU/GPU sensor the fans go to
///   full speed (not to auto, which might spin slower than the setting), and back below 85 °C;
/// - on SIGTERM/SIGINT (launchd unload, uninstall) fans we set go back to macOS before exiting;
/// - the state file is written atomically by rename, so a symlink planted in the user's folder
///   can't make root write anywhere else.
final class Daemon {
    private let fans: Fans
    private let configPath: String
    private let statePath: String
    private var picked: Fans.Mode?
    private var applied: Fans.Mode?
    private var boosting = false
    private var conflict = false
    private var history: [[Double]] = []
    private var signals: [DispatchSourceSignal] = []
    private var timer: DispatchSourceTimer?

    init(fans: Fans, config: String, state: String) {
        self.fans = fans
        configPath = config
        statePath = state
    }

    func run() -> Never {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                MainActor.assumeIsolated {
                    if case .fixed = self.applied, !self.conflict { try? self.fans.apply(.auto) }
                    exit(0)
                }
            }
            source.resume()
            signals.append(source)
        }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: .seconds(2))
        timer.setEventHandler { MainActor.assumeIsolated { self.tick() } }
        timer.resume()
        self.timer = timer
        dispatchMain()
    }

    /// The mode picked in the panel; nil when nothing was picked yet (hands off).
    private func wanted() -> Fans.Mode? {
        guard let data = FileManager.default.contents(atPath: configPath),
              let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let mode = config["mode"] as? String
        else { return nil }
        if mode == "fixed", let percent = config["percent"] as? Int, (30...100).contains(percent) {
            return .fixed(percent: percent)
        }
        return .auto  // anything else, including a bad percent, gives control back to macOS
    }

    private func tick() {
        var reading = fans.reading()
        let mode = wanted()
        if mode != picked {
            picked = mode
            conflict = false  // a new pick in the panel takes the fans back
        }
        if let mode {
            let hottest = max(reading.cpu ?? 0, reading.gpu ?? 0)
            if hottest >= 95 { boosting = true } else if hottest < 85 { boosting = false }
            let target: Fans.Mode = mode != .auto && boosting ? .fixed(percent: 100) : mode
            if target != applied {
                write(target, &reading)
            } else if !conflict, !fans.holds(target, reading) {
                // fans back on auto under a fixed setting: the SMC forgot it over sleep
                if case .fixed = target, reading.fans.allSatisfy({ !$0.manual }) {
                    write(target, &reading)
                } else {
                    conflict = true
                }
            }
            switch mode {
            case .auto:
                reading.mode = "auto"
            case .fixed(let percent):
                reading.mode = "fixed"
                reading.percent = percent
            }
            reading.boosting = boosting && mode != .auto
        }
        reading.conflict = conflict
        if (history.last?.first).map({ reading.at - $0 >= 5 }) ?? true {
            let rpm = reading.fans.isEmpty ? 0 : reading.fans.map(\.rpm).reduce(0, +) / Double(reading.fans.count)
            history.append([reading.at.rounded(), reading.cpu ?? 0, reading.gpu ?? 0, rpm.rounded()])
            if history.count > 240 { history.removeFirst(history.count - 240) }
        }
        reading.history = history
        save(reading)
    }

    private func write(_ target: Fans.Mode, _ reading: inout Reading) {
        do {
            try fans.apply(target)
            applied = target
            reading = fans.reading()
        } catch {
            reading.error = "\(error)"
            try? fans.apply(.auto)
            applied = .auto
        }
    }

    private func save(_ reading: Reading) {
        guard let data = try? JSONEncoder.pretty.encode(reading) else { return }
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
        chmod(statePath, 0o644)
    }
}

extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}
