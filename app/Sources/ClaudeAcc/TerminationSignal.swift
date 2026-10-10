import Dispatch
import Darwin

/// SIGTERM as a normal quit. Without it the default action ends the process on the spot, so
/// `applicationWillTerminate` never runs: no hand-back of the menu bar item, dictation cut off.
/// launchd sends it when Pod unregisters its login item, and Pod's updater sends it before it
/// replaces the bundle Pod Menu runs from.
final class TerminationSignal {
    private let source: DispatchSourceSignal

    /// Calls `quit` on `queue` for every SIGTERM from now on, instead of dying.
    init(queue: DispatchQueue = .main, quit: @escaping @Sendable () -> Void) {
        signal(SIGTERM, SIG_IGN)
        source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: queue)
        source.setEventHandler(handler: quit)
        source.resume()
    }

    /// Back to the default action (for tests).
    func cancel() {
        source.cancel()
        signal(SIGTERM, SIG_DFL)
    }
}
