import PodRootdProtocol
import ServiceManagement

/// The helper as a launch daemon of Pod.app (docs/pod-rootd.md, "Approval UX"). Only Pod.app's own
/// process can register it: `SMAppService.daemon(plistName:)` looks for the plist in the calling
/// app's Contents/Library/LaunchDaemons.
public struct PodRootdService: Sendable {
    public let plistName: String

    public init(appIdentifier: String = PodRootd.appIdentifier) {
        plistName = PodRootd.plistName(appIdentifier: appIdentifier)
    }

    /// What the UI does next.
    public enum Step: Sendable, Equatable {
        /// Not registered: register when the user turns on something that needs root.
        case register
        /// Registered, waiting for an admin's approval in Login Items (or the approval was revoked).
        case approve
        /// Running or ready to run: connect.
        case ready
        /// The bundle has no such plist.
        case damaged
    }

    public var status: SMAppService.Status { SMAppService.daemon(plistName: plistName).status }

    public var step: Step {
        switch status {
        case .notRegistered: .register
        case .requiresApproval: .approve
        case .enabled: .ready
        case .notFound: .damaged
        @unknown default: .damaged
        }
    }

    /// Registers the daemon; the system starts it after an admin approves it, then at every boot.
    public func register() throws {
        try SMAppService.daemon(plistName: plistName).register()
    }

    /// Stops and unregisters it. Send `restoreDefaults` first: SIGTERM alone keeps sysctls until the
    /// next boot and the Spotlight list and power mode for good.
    public func unregister() throws {
        try SMAppService.daemon(plistName: plistName).unregister()
    }

    /// System Settings at Login Items, where the user allows the helper.
    public static func openLoginItems() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
