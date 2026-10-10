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

    /// The job's plist name; the package installs it in /Library/LaunchDaemons.
    public static func plistName(appIdentifier: String = appIdentifier) -> String {
        serviceName(appIdentifier: appIdentifier) + ".plist"
    }

    // The package's install layout (docs/pod-rootd.md, "Install layout"): root-only directories, so
    // nothing running as the user can swap the program or edit the job.

    /// The helper as launchd runs it.
    public static func installedProgram(appIdentifier: String = appIdentifier) -> String {
        "/Library/PrivilegedHelperTools/" + serviceName(appIdentifier: appIdentifier)
    }

    /// The job.
    public static func installedPlist(appIdentifier: String = appIdentifier) -> String {
        "/Library/LaunchDaemons/" + plistName(appIdentifier: appIdentifier)
    }

    /// The package receipt, for `pkgutil --forget` and the cask's `uninstall pkgutil:`.
    public static let packageIdentifier = "codes.pod.rootd.pkg"
    /// The package's name in Contents/Resources/claude-acc of Pod.app, next to pod-rootd and pod-rootctl.
    public static let packageName = "pod-rootd.pkg"
    /// Where pod-rootd and the package sit inside Pod.app.
    public static let bundleDirectory = "Contents/Resources/claude-acc"

    /// What the helper must be, as code signing language: Developer ID of Pod's team, its identifier.
    /// A self-update also requires `notarized` of the copy it takes.
    public static func helperRequirementText(team: String = teamIdentifier, notarized: Bool = false) -> String {
        "anchor apple generic and certificate leaf[subject.OU] = \"\(team)\" and identifier \"\(helperIdentifier)\""
            + (notarized ? " and notarized" : "")
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
