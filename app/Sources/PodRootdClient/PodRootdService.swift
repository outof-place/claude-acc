import Foundation
import PodRootdProtocol
import ServiceManagement

/// The helper as Pod's package installs it (docs/pod-rootd.md, "Installing and removing it"): a
/// launchd job in /Library/LaunchDaemons that Login Items lists under Pod. Pod doesn't register
/// anything; the user opens the signed package from Pod.app and an administrator authorizes it.
public struct PodRootdService: Sendable {
    public let plist: URL

    public init(appIdentifier: String = PodRootd.appIdentifier) {
        plist = URL(fileURLWithPath: PodRootd.installedPlist(appIdentifier: appIdentifier))
    }

    /// What the UI does next.
    public enum Step: Sendable, Equatable {
        /// Not installed: offer the package when the user turns on something that needs root.
        case install
        /// Installed, but switched off in Login Items: offer System Settings.
        case approve
        /// Installed and allowed: connect.
        case ready
    }

    /// Apple's status for a helper outside the app bundle (`statusForLegacyPlist(at:)`).
    public var status: SMAppService.Status { SMAppService.statusForLegacyPlist(at: plist) }

    public var step: Step { Self.step(for: status) }

    static func step(for status: SMAppService.Status) -> Step {
        switch status {
        case .enabled: .ready
        case .requiresApproval: .approve
        case .notRegistered, .notFound: .install
        @unknown default: .install
        }
    }

    /// The signed package inside a Pod.app, next to pod-rootd and pod-rootctl.
    public static func package(in app: URL) -> URL {
        app.appendingPathComponent(PodRootd.bundleDirectory).appendingPathComponent(PodRootd.packageName)
    }

    /// System Settings at Login Items, where the user switches the helper back on.
    public static func openLoginItems() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
