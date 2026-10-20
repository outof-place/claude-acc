import Darwin
import Foundation

/// `ClaudeAcc --browser-hub` (browser.py `hub_host`): the browser gateway's daemon, `browser.py serve`, as a
/// child of this app's own executable, which first makes itself its own responsible process. macOS judges
/// the daemon's reads of Chrome's and Brave's profile folders by the responsible process's grants, so Full
/// Disk Access belongs to this app, once. Started directly, the daemon answered to whichever agent terminal
/// started it first (Pod's terminal daemon, in a folder every Pod update renames), and the grant broke.
/// Runs before SwiftUI: no window server connection, no menu bar ring.
enum BrowserHubHost {
    static let flag = "--browser-hub"
    /// Set before the disclaiming re-exec, so a re-exec that lands without its own responsibility doesn't loop.
    static let disclaimedKey = "CLAUDE_ACC_BROWSER_HUB_DISCLAIMED"

    static func requested(_ arguments: [String]) -> Bool { arguments.dropFirst().first == flag }

    /// The installed daemon, run by the interpreter the app's own scripts use. Fixed, not taken from the
    /// command line: this app's grants shouldn't be a door any program can walk through.
    static func command(directory: String = CLI.directory, python: String = CLI.python) -> [String] {
        [python, directory + "/browser.py", "serve"]
    }

    static func run() -> Never {
        let me = getpid()
        if getenv(disclaimedKey) == nil, Responsibility.of(me) != me {
            setenv(disclaimedKey, "1", 1)
            let reason = Responsibility.execDisclaimed(CommandLine.unsafeArgv)
            note("browser.py starts answering to the process that started it (\(reason))")
        }
        unsetenv(disclaimedKey)
        exit(runChild(command()))
    }

    /// Spawns argv with this process's stdio and environment, forwards SIGTERM, SIGINT and SIGHUP to it,
    /// and returns its exit code (128 + the signal when a signal ended it, 127 when it couldn't start).
    static func runChild(_ argv: [String]) -> Int32 {
        let args: [UnsafeMutablePointer<CChar>?] = argv.map { strdup($0) } + [nil]
        defer { args.forEach { free($0) } }
        var pid: pid_t = 0
        let spawned = posix_spawn(&pid, argv[0], nil, nil, args, environ)
        guard spawned == 0 else {
            note("\(argv[0]): \(String(cString: strerror(spawned)))")
            return 127
        }
        let forwarded = [SIGTERM, SIGINT, SIGHUP]
        hubChild = pid
        forwarded.forEach { Darwin.signal($0, forwardToHub) }
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0, errno == EINTR {}
        // the pid is free for reuse now: nothing may be forwarded to it
        hubChild = 0
        forwarded.forEach { Darwin.signal($0, SIG_DFL) }
        return exitCode(status)
    }

    nonisolated static func exitCode(_ status: Int32) -> Int32 {
        status & 0x7f == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
    }

    /// One line in hub.log, which is this process's stderr.
    private static func note(_ text: String) {
        FileHandle.standardError.write(Data("claude-acc-browser-hub: \(text)\n".utf8))
    }
}

/// The daemon's pid for the signal handler, written once before the handlers go in.
private nonisolated(unsafe) var hubChild: pid_t = 0

private nonisolated func forwardToHub(_ signal: Int32) {
    if hubChild > 0 { kill(hubChild, signal) }
}

/// libquarantine's responsibility SPI, which has no header; Chromium (base/process/launch_mac.cc) and
/// Electron's utilityProcess `disclaim` call it the same way.
nonisolated enum Responsibility {
    private typealias ResponsibleFor = @convention(c) (pid_t) -> pid_t
    private typealias SetDisclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    // <dlfcn.h> RTLD_DEFAULT, a macro Swift does not import
    private static var rtldDefault: UnsafeMutableRawPointer? { UnsafeMutableRawPointer(bitPattern: -2) }

    /// The process whose grants macOS checks when pid reads protected data; pid itself when unknown.
    static func of(_ pid: pid_t) -> pid_t {
        guard let symbol = dlsym(rtldDefault, "responsibility_get_pid_responsible_for_pid") else { return pid }
        return unsafeBitCast(symbol, to: ResponsibleFor.self)(pid)
    }

    /// Execs this executable again in place (same pid, same argv and environment), responsible for itself.
    /// Returns only on failure, with the reason.
    static func execDisclaimed(_ argv: UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> String {
        guard let symbol = dlsym(rtldDefault, "responsibility_spawnattrs_setdisclaim") else {
            return "responsibility_spawnattrs_setdisclaim is unavailable"
        }
        var path = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
        guard proc_pidpath(getpid(), &path, UInt32(path.count)) > 0 else { return "no path for this executable" }
        var attributes: posix_spawnattr_t?
        var result = posix_spawnattr_init(&attributes)
        guard result == 0 else { return String(cString: strerror(result)) }
        defer { posix_spawnattr_destroy(&attributes) }
        result = posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETEXEC))
        if result == 0 { result = unsafeBitCast(symbol, to: SetDisclaim.self)(&attributes, 1) }
        if result == 0 {
            var pid: pid_t = 0
            result = posix_spawn(&pid, path, nil, &attributes, argv, environ)
        }
        return String(cString: strerror(result))
    }
}
