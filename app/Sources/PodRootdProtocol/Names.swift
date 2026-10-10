/// Names and identities pod-rootd and its clients agree on (docs/pod-rootd.md).
public enum PodRootd {
    /// Bumped when a verb or a reply changes shape; the helper refuses any other version.
    public static let protocolVersion = 1
    /// The signing team of Pod and of everything it ships.
    public static let teamIdentifier = "75Y2KR6P5W"
    /// The helper's signing identifier; clients require it of the service they reach.
    public static let helperIdentifier = "codes.pod.rootd"
    /// Pod's bundle id: the helper's label and Mach service hang off it.
    public static let appIdentifier = "codes.pod.app"
    /// Pod Menu keeps ClaudeAcc's bundle id, so its Microphone and Accessibility grants hold.
    public static let menuIdentifier = "com.filip.claude-acc.menubar"
    /// The CLI the scripts run (`pod-rootctl`).
    public static let cliIdentifier = "codes.pod.rootctl"

    /// Label and Mach service of the helper in the Pod build with this bundle id.
    public static func serviceName(appIdentifier: String = appIdentifier) -> String {
        appIdentifier + ".rootd"
    }

    /// The plist in Contents/Library/LaunchDaemons: the name `SMAppService.daemon(plistName:)` takes.
    public static func plistName(appIdentifier: String = appIdentifier) -> String {
        serviceName(appIdentifier: appIdentifier) + ".plist"
    }
}

/// Who called, as the helper's peer requirement told them apart: each is one signing identifier
/// of team `PodRootd.teamIdentifier`.
public enum Caller: String, Codable, Sendable, CaseIterable {
    /// Pod.app's main process.
    case app
    /// Pod Menu, the menu bar helper.
    case menu
    /// `pod-rootctl`, which the scripts run.
    case cli

    public var signingIdentifier: String {
        switch self {
        case .app: PodRootd.appIdentifier
        case .menu: PodRootd.menuIdentifier
        case .cli: PodRootd.cliIdentifier
        }
    }
}

/// The five root LaunchDaemons claude-acc installed before pod-rootd.
public enum LegacyDaemon: String, Codable, Sendable, CaseIterable {
    case fans = "com.filip.claude-acc.fans"
    case fsguard = "com.filip.claude-acc.fsguard"
    case hotspot = "com.filip.claude-acc.hotspot"
    case iogpu = "com.filip.claude-acc.iogpu"
    case vnodes = "com.filip.claude-acc.vnodes"

    public var plistPath: String { "/Library/LaunchDaemons/\(rawValue).plist" }
}
