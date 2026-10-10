import Foundation
import SMCKit

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
/// - Stay Awake with the lid closed rides along: `Lid` follows the app's `awake.json`;
/// - the state file is written atomically by rename, so a symlink planted in the user's folder
///   can't make root write anywhere else.
///
/// Readings go to the state file with each chart sample (every 6 s); what the panel reacts to
/// (mode, boost, conflict, error, a fan going manual) goes out on the tick it happens. Writing
/// the whole history every 2 s cost ~890 MB of writes a day. Between samples only a fixed
/// setting below 100% reads the temperature sensors (the 95 °C rule); the rest only checks the fans.
final class Daemon {
    private let fans: Fans
    private let lid: Lid
    private let configPath: String
    private let statePath: String
    private var picked: Fans.Mode?
    private var applied: Fans.Mode?
    private var boosting = false
    private var conflict = false
    private var history: [[Double]] = []
    /// Last temperatures read, carried into readings taken without the sensors.
    private var sensors: (all: [String: Double], cpu: Double?, gpu: Double?) = ([:], nil, nil)
    /// Uptime of the last sample: a wall clock set back must not stop the writes.
    private var sampledAt = -Double.infinity
    /// What the state file holds, to tell a change the panel shows from new numbers.
    private var saved: Reading?
    private var signals: [DispatchSourceSignal] = []
    private var timer: DispatchSourceTimer?
    /// Seconds between chart samples; with the 2 s tick a sample lands every 6 s.
    private static let sample: Double = 5

    init(fans: Fans, config: String, state: String, teams: Set<String> = []) {
        self.fans = fans
        configPath = config
        statePath = state
        // `SleepDisabled` outlives a restart: the last state file says whether it was ours
        let previous = FileManager.default.contents(atPath: state).flatMap { try? JSONDecoder().decode(Reading.self, from: $0) }
        let folder = (config as NSString).deletingLastPathComponent
        lid = Lid(requestPath: folder + "/awake.json", heldBefore: previous?.lidHeld == true, teams: teams)
    }

    func run() -> Never {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler {
                MainActor.assumeIsolated {
                    if case .fixed = self.applied, !self.conflict { try? self.fans.apply(.auto) }
                    self.lid.release()
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
        let mode = wanted()
        let uptime = ProcessInfo.processInfo.systemUptime
        let sampling = uptime - sampledAt >= Self.sample
        // a fixed setting below full speed needs the temperatures every tick for the 95 °C rule;
        // at 100% the boost changes nothing but its label, which can wait for the next sample
        var measured = sampling
        if case .fixed(let percent)? = mode, percent < 100 { measured = true }
        var reading = fans.reading(sensors: measured)
        if measured {
            sensors = (reading.sensors, reading.cpu, reading.gpu)
        } else {
            (reading.sensors, reading.cpu, reading.gpu) = sensors
        }
        if mode != picked {
            picked = mode
            conflict = false  // a new pick in the panel takes the fans back
        }
        if let mode {
            if measured {
                let hottest = max(reading.cpu ?? 0, reading.gpu ?? 0)
                if hottest >= 95 { boosting = true } else if hottest < 85 { boosting = false }
            }
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
        lid.tick()
        reading.lidHeld = lid.held ? true : nil
        if sampling {
            sampledAt = uptime
            let rpm = reading.fans.isEmpty ? 0 : reading.fans.map(\.rpm).reduce(0, +) / Double(reading.fans.count)
            let tenth = { (value: Double?) in ((value ?? 0) * 10).rounded() / 10 }
            history.append([reading.at.rounded(), tenth(reading.cpu), tenth(reading.gpu), rpm.rounded()])
            if history.count > 240 { history.removeFirst(history.count - 240) }
        }
        reading.history = history
        if sampling || shows(reading) { save(reading) }
    }

    /// Does the panel show something new beyond the numbers: mode, the boost, a conflict, an
    /// error, a fan taken manual or given back.
    private func shows(_ reading: Reading) -> Bool {
        guard let saved else { return true }
        return reading.mode != saved.mode || reading.percent != saved.percent
            || reading.boosting != saved.boosting || reading.conflict != saved.conflict
            || reading.error != saved.error || reading.fans.map(\.manual) != saved.fans.map(\.manual)
            || reading.lidHeld != saved.lidHeld
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
        // compact: the history is 240 rows, pretty printing tripled the file to ~20 KB
        guard let data = try? JSONEncoder.compact.encode(reading),
              (try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)) != nil
        else { return }
        chmod(statePath, 0o644)
        saved = reading
    }
}

extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }

    static let compact: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()
}
