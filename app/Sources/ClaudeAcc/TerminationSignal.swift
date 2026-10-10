import Dispatch
import Darwin

/// SIGTERM as a normal quit. Without it the default action ends the process on the spot, so
/// `applicationWillTerminate` never runs: no hand-back of the menu bar item, dictation cut off.
/// launchd sends it when Pod unregisters its login item, and Pod's updater sends it before it
/// replaces the bundle Pod Menu runs from.
// nonisolated: the handler runs on `queue`, not necessarily the main actor (the target defaults to it)
nonisolated final class TerminationSignal: @unchecked Sendable {
    private let source: DispatchSourceSignal
    // touched only on `queue`
    private var received = false

    /// Calls `quit` on `queue` at the first SIGTERM instead of dying. A quit that hasn't ended the
    /// process `grace` later (applicationWillTerminate stuck on menubar.lock, say), or a second
    /// SIGTERM, calls `exit`: whoever sent it expects the process gone.
    init(
        queue: DispatchQueue = .main, grace: DispatchTimeInterval = .seconds(5),
        exit: @escaping @Sendable () -> Void = { _exit(0) }, quit: @escaping @Sendable () -> Void
    ) {
        signal(SIGTERM, SIG_IGN)
        source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: queue)
        source.setEventHandler { [unowned self] in
            guard !received else { return exit() }
            received = true
            queue.asyncAfter(deadline: .now() + grace, execute: exit)
            quit()
        }
        source.resume()
    }

    /// Back to the default action (for tests).
    func cancel() {
        source.cancel()
        signal(SIGTERM, SIG_DFL)
    }
}
