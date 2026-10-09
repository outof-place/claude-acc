import Foundation
import IOKit
import IOKit.ps
import Security

/// Stay Awake with the lid closed. A power assertion only stops idle sleep: closing the lid of a
/// MacBook on battery still sleeps it (`Clamshell Sleep` in `pmset -g log`). The one switch that
/// stops that is the system-wide `SleepDisabled` (`pmset disablesleep`), and it needs root, so the
/// daemon holds it for the app.
///
/// Rules:
/// - the app asks through `awake.json` next to `fans.json`: `{"lid": true, "until": <epoch>,
///   "pid": <its pid>}`. The request holds only while `until` is ahead and that pid is still the
///   running app (`Requester`), so a quit or crashed app can't leave the Mac unable to sleep;
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
    /// Signing teams the app may come with, besides `Requester.team` (`daemon --team`).
    private let teams: Set<String>
    /// `SleepDisabled` is on because this daemon turned it on.
    private(set) var held: Bool
    /// The longest a single request keeps the lid from sleeping the Mac, counted from its write.
    static let maxHold: TimeInterval = 24 * 3600
    /// After a thermal let-go the lid is not held again for this long.
    static let thermalPause: TimeInterval = 15 * 60
    private var cooledUntil: Date = .distantPast

    init(requestPath: String, heldBefore: Bool, teams: Set<String> = []) {
        self.requestPath = requestPath
        self.teams = teams
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
        return Requester.trusted(pid: pid, teams: teams)
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

/// Who may hold the lid: the menu bar app, known by its code signature rather than its process
/// name, so it still counts once it ships under another name (Pod Menu.app in Pod).
///
/// - signed by a team: the team must be `team` or one named at install (`fanctl daemon --team`, for
///   a build signed with someone else's certificate), and the running code must satisfy
///   `anchor apple generic and certificate leaf[subject.OU] = <team>`: a self-made certificate can
///   claim any team id, Apple's chain can't be faked;
/// - signed without a team (ad hoc: `swift build`, an install without a certificate) or not at all:
///   the process name, `ClaudeAcc`, as before;
/// - a signature that doesn't hold (the binary changed under the process) or a pid that's gone: no.
///
/// A pid from a file can be reused; the app writes its request again while it runs, and `until`
/// and `Lid.maxHold` bound a stale one, as they did with the name check.
enum Requester {
    /// The team that signs Claude Acc and Pod.
    static let team = "75Y2KR6P5W"
    static let name = "ClaudeAcc"

    enum Signer: Equatable, CustomStringConvertible {
        /// Valid signature from Apple's chain with this team.
        case team(String)
        /// Valid ad hoc signature, or none at all.
        case noTeam
        /// No such process, or its signature doesn't hold.
        case invalid(OSStatus)

        var description: String {
            switch self {
            case .team(let id): "team \(id)"
            case .noTeam: "no team (ad hoc or unsigned)"
            case .invalid(let status): "invalid (OSStatus \(status))"
            }
        }
    }

    static func trusted(pid: pid_t, teams: Set<String>) -> Bool {
        switch signer(pid: pid) {
        case .team(let id): teams.union([team]).contains(id)
        case .noTeam: processName(pid) == name
        case .invalid: false
        }
    }

    static func signer(pid: pid_t) -> Signer {
        var found: SecCode?
        let attributes = [kSecGuestAttributePid: NSNumber(value: pid)] as CFDictionary
        var status = SecCodeCopyGuestWithAttributes(nil, attributes, [], &found)
        guard status == errSecSuccess, let code = found else { return .invalid(status) }
        status = SecCodeCheckValidity(code, [], nil)
        if status == errSecCSUnsigned { return .noTeam }
        guard status == errSecSuccess else { return .invalid(status) }
        var staticCode: SecStaticCode?
        var info: CFDictionary?
        status = SecCodeCopyStaticCode(code, [], &staticCode)
        guard status == errSecSuccess, let staticCode else { return .invalid(status) }
        status = SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info)
        guard status == errSecSuccess else { return .invalid(status) }
        guard let id = (info as? [String: Any])?[kSecCodeInfoTeamIdentifier as String] as? String, !id.isEmpty,
              id.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber) })
        else { return .noTeam }
        var requirement: SecRequirement?
        let text = "anchor apple generic and certificate leaf[subject.OU] = \"\(id)\"" as CFString
        status = SecRequirementCreateWithString(text, [], &requirement)
        guard status == errSecSuccess, let requirement else { return .invalid(status) }
        status = SecCodeCheckValidity(code, [], requirement)
        return status == errSecSuccess ? .team(id) : .invalid(status)
    }

    static func processName(_ pid: pid_t) -> String? {
        var name = [CChar](repeating: 0, count: 64)
        guard proc_name(pid, &name, UInt32(name.count)) > 0 else { return nil }
        return String(decoding: name.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }
}
