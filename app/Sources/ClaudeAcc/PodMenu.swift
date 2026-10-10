import AppKit

/// Pod Menu: this app inside Pod.app (Contents/Library/LoginItems), which Pod registers as its login
/// item through SMAppService and whose launchd agents are Pod's (codes.pod.app.acc.*). Pod owns the
/// start at login, so the app neither registers itself nor shows the switch for it.
enum PodMenu {
    static func isLoginItem(_ url: URL?) -> Bool {
        url?.path.contains("/Contents/Library/LoginItems/") ?? false
    }

    static let active = isLoginItem(Bundle.main.bundleURL)

    /// The launchd label of a claude-acc job ("devguard") as `launchctl print` names it.
    static func agentLabel(_ job: String) -> String {
        active ? "codes.pod.app.acc.\(job)" : "com.filip.claude-acc.\(job)"
    }

    /// One ring in the menu bar. Pod Menu wins over a legacy ~/Applications copy (its old login
    /// item may still start it after setup.sh --pod-agents removed it); otherwise the newcomer leaves.
    static func keepOneCopy() {
        let mine = Bundle.main.bundleIdentifier ?? ""
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: mine)
            .filter { $0 != NSRunningApplication.current }
        if active {
            for other in others where !isLoginItem(other.bundleURL) {
                other.terminate()
            }
            if others.contains(where: { isLoginItem($0.bundleURL) }) { exit(0) }
        } else if !others.isEmpty {
            exit(0)
        }
    }
}
