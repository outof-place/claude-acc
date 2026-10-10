import AccKit
import Foundation

/// Whether Pod Menu's ring is on the menu bar.
///
/// Pod's nested copy (PodMenu.active) takes part in the hand-over with Pod's native shell, through
/// $STATE/menubar.json (AccKit's AccMenuBarCoordinator). It shows the ring only while the hand-over
/// says so, and keeps running without it.
///
/// The standalone copy (~/Applications/Claude Acc.app from brew or install.sh) stays out of it and
/// always shows the ring. That way nothing another app writes can ever take the only claude-acc
/// ring off the bar. Pod's native shell sees it as a Pod Menu that does not take part, and stays off.
final class MenuBarPresence {
    private let store: AccStore?
    private let handover: AccMenuBarCoordinator?
    private let show: @MainActor (Bool) -> Void

    /// `show` puts the ring on the bar or takes it off (MenuBarController.setShown).
    convenience init(nested: Bool = PodMenu.active, show: @escaping @MainActor (Bool) -> Void) {
        guard nested else {
            self.init(store: nil, handover: nil, show: show)
            return
        }
        // menubar.json only: the rest of $STATE is Store's
        let store = AccStore(paths: .account, interest: [.menuBar])
        self.init(store: store, handover: AccMenuBarCoordinator(store: store, role: .podMenu, show: show), show: show)
    }

    /// With the store and coordinator given, for tests (a temporary $STATE, fake parties).
    init(store: AccStore?, handover: AccMenuBarCoordinator?, show: @escaping @MainActor (Bool) -> Void) {
        self.store = store
        self.handover = handover
        self.show = show
    }

    var takesPart: Bool { handover != nil }

    func start() {
        guard let store, let handover else {
            show(true)
            return
        }
        store.start()
        handover.start()
    }

    /// On quit: the ring comes off first, then the hand-over lets go of it.
    func stop() {
        handover?.stop()
        store?.stop()
    }
}
