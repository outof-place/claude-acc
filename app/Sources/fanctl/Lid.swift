import Foundation
import IOKit
import IOKit.ps

/// Stay Awake with the lid closed. A power assertion only stops idle sleep: closing the lid of a
/// MacBook on battery still sleeps it (`Clamshell Sleep` in `pmset -g log`). The one switch that
/// stops that is the system-wide `SleepDisabled` (`pmset disablesleep`), and it needs root, so the
/// daemon holds it for the app.
///
/// Rules:
/// - the app asks through `awake.json` next to `fans.json`: `{"lid": true, "until": <epoch>,
///   "pid": <its pid>}`. The request holds only while `until` is ahead and that pid is still a
///   running ClaudeAcc, so a quit or crashed app can't leave the Mac unable to sleep;
/// - on battery at 10% or less it lets go, so a closed Mac in a bag sleeps before it dies;
/// - at a serious thermal state or worse it lets go and stays off for `thermalPause`, so a hot
///   Mac in a bag sleeps instead of cooking;
/// - a request holds for at most `maxHold` after the app wrote it, even for a session without
///   an end: a Mac nobody touches for a day is allowed to sleep;
/// - it turns `SleepDisabled` off only when it turned it on itself: someone else's setting
///   (Amphetamine, `sudo pmset disablesleep 1`) is left alone. The setting survives a reboot, so
///   that it holds it is kept in the state file and read back on start;
/// - on SIGTERM/SIGINT it lets go before exiting.
final class Lid {
    private let requestPath: String
    /// `SleepDisabled` is on because this daemon turned it on.
    private(set) var held: Bool
    /// The longest a single request keeps the lid from sleeping the Mac, counted from its write.
    static let maxHold: TimeInterval = 24 * 3600
    /// After a thermal let-go the lid is not held again for this long.
    static let thermalPause: TimeInterval = 15 * 60
    private var cooledUntil: Date = .distantPast

    init(requestPath: String, heldBefore: Bool) {
        self.requestPath = requestPath
        held = heldBefore
    }

    func tick() {
        if Self.tooHot() { cooledUntil = Date.now.addingTimeInterval(Self.thermalPause) }
        let wanted = requested() && !batteryLow() && Date.now >= cooledUntil
        if wanted, !held, !Self.sleepDisabled() {
            if Self.setSleepDisabled(true) { held = true }
        } else if !wanted, held {
            if Self.setSleepDisabled(false) { held = false }
        } else if held, !Self.sleepDisabled() {
            held = false  // turned off by hand: take it again on the next tick if still wanted
        }
    }

    func release() {
        if held, Self.setSleepDisabled(false) { held = false }
    }

    /// The app's request, valid only while its session lasts and the app that wrote it runs.
    private func requested() -> Bool {
        guard let data = FileManager.default.contents(atPath: requestPath),
              let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              request["lid"] as? Bool == true,
              let until = request["until"] as? Double, until > Date.now.timeIntervalSince1970,
              let pid = request["pid"] as? Int32, pid > 1,
              let written = (try? FileManager.default.attributesOfItem(atPath: requestPath))?[.modificationDate] as? Date,
              Date.now.timeIntervalSince(written) < Self.maxHold
        else { return false }
        var name = [CChar](repeating: 0, count: 64)
        guard proc_name(pid, &name, UInt32(name.count)) > 0 else { return false }
        return String(cString: name) == "ClaudeAcc"
    }

    /// Running on battery with 10% or less left.
    private func batteryLow() -> Bool {
        guard let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let source = IOPSGetProvidingPowerSourceType(blob)?.takeUnretainedValue() as String?,
              source == kIOPSBatteryPowerValue,
              let list = IOPSCopyPowerSourcesList(blob)?.takeRetainedValue() as? [CFTypeRef]
        else { return false }
        for item in list {
            guard let info = IOPSGetPowerSourceDescription(blob, item)?.takeUnretainedValue() as? [String: Any],
                  let current = info[kIOPSCurrentCapacityKey] as? Int,
                  let max = info[kIOPSMaxCapacityKey] as? Int, max > 0
            else { continue }
            if current * 100 / max <= 10 { return true }
        }
        return false
    }

    /// macOS's own thermal verdict: `.serious` already throttles the chip, the fans can't keep up.
    static func tooHot() -> Bool {
        ProcessInfo.processInfo.thermalState.rawValue >= ProcessInfo.ThermalState.serious.rawValue
    }

    /// The root power domain publishes the setting, so reading it spawns nothing.
    static func sleepDisabled() -> Bool {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard root != 0 else { return false }
        defer { IOObjectRelease(root) }
        let value = IORegistryEntryCreateCFProperty(root, "SleepDisabled" as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue()
        return (value as? Bool) ?? false
    }

    private static func setSleepDisabled(_ on: Bool) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pmset")
        process.arguments = ["-a", "disablesleep", on ? "1" : "0"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return false }
        process.waitUntilExit()
        return process.terminationStatus == 0
    }
}
