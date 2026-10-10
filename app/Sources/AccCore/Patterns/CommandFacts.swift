// What the guard's patterns say about one command line, each asked at most once per line. A process
// keeps its facts as long as its line stays (ProcCache), so a tick runs the patterns only on new
// processes: on this Mac a full pass over the table costs 25 ms of ICU, a cached one microseconds.

public final class CommandFacts: @unchecked Sendable {
    public let command: String

    public init(_ command: String) { self.command = command }

    /// os.path.basename(command.split(None, 1)[0])
    public private(set) lazy var program: String = basename(firstWord(command))
    /// the first of SERVER_KINDS that matches (asked only of runtime processes)
    public private(set) lazy var serverKind: String? = GuardPatterns.serverKinds.first(where: { $0.1.search(command) })?.0
    public private(set) lazy var launcher: Bool = GuardPatterns.launcher.search(command)
    public private(set) lazy var shell: Bool = GuardPatterns.shell.match(command)
    public private(set) lazy var agent: Bool = GuardPatterns.agent.search(command)
    public private(set) lazy var sacred: Bool = GuardContext.host.sacred.search(command)
    public private(set) lazy var family: String = GuardPatterns.families.first(where: { $0.1.search(command) })?.0 ?? "rest"
    public private(set) lazy var launchdSim: Bool = GuardPatterns.launchdSim.search(command)
    /// the UDID in a /CoreSimulator/Devices/<UDID>/ path
    public private(set) lazy var simDevice: String? = GuardPatterns.simDevice.groups(command)?.first ?? nil
    /// any UDID at all, and a viewer of every booted simulator (serve-sim, simctl … booted)
    public private(set) lazy var hasUdid: Bool = GuardPatterns.udidRx.search(command)
    public private(set) lazy var simViewer: Bool = GuardPatterns.serveSim.search(command) || GuardPatterns.simctlBooted.search(command)

    /// client_kind
    public private(set) lazy var clientKind: String = {
        if simDevice != nil || GuardPatterns.simDevice.search(command) { return "simulator" }
        if GuardContext.host.apps.search(command) { return "orca" }
        if GuardPatterns.headless.search(command) { return "headless" }
        if GuardPatterns.browser.search(command) { return "browser" }
        return "tool"
    }()
}
