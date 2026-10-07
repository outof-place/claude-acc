import Foundation

nonisolated struct CLIResult: Sendable {
    let status: Int32
    let stdout: String
    let stderr: String

    /// Last non-empty line of output: that is where the scripts print their result or error.
    var message: String {
        let lines = (stdout + "\n" + stderr).split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        let text = lines.last { !$0.isEmpty } ?? ""
        for prefix in ["błąd: ", "error: "] where text.hasPrefix(prefix) {
            return String(text.dropFirst(prefix.count))
        }
        return text
    }
}

/// The Python scripts the launchd jobs run: the app shows their state and calls their commands.
enum CLI {
    static let directory = NSHomeDirectory() + "/.local/share/claude-acc"
    static let accounts = directory + "/accswitch.py"
    static let janitor = directory + "/janitor.py"
    static let devguard = directory + "/devguard.py"
    static let perf = directory + "/perf.py"
    static let sched = directory + "/sched.py"
    static let updates = directory + "/updates.py"
    static let mail = directory + "/mail.py"
    static let janitorState = directory + "/janitor-state.json"
    static let guardState = directory + "/devguard-state.json"
    static let guardConfig = directory + "/devguard.json"
    static let fanConfig = directory + "/fans.json"
    static let fanState = directory + "/fans-state.json"
    static let perfState = directory + "/perf-state.json"
    static let schedState = directory + "/sched/state.json"
    static let depotState = directory + "/sched/depot.json"
    static let updatesState = directory + "/updates-state.json"
    static let mailPanel = directory + "/mail/panel.json"
    static let switchLog = directory + "/switch.log"
    static let janitorLog = directory + "/janitor.log"
    static let guardLog = directory + "/devguard.log"
    static let updatesLog = directory + "/updates.log"

    /// The interpreter setup.sh links (uv's CPython), or the system one.
    static let python: String = {
        let linked = directory + "/python"
        return FileManager.default.isExecutableFile(atPath: linked) ? linked : "/usr/bin/python3"
    }()

    /// acc.py runs a script from cached bytecode: Python compiles the file it is given on
    /// every start, 7-15 ms of each `status --json` the app asks for.
    static let launcher = directory + "/acc.py"

    static func process(_ args: [String], script: String = accounts) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        let name = (script as NSString).lastPathComponent
        if name.hasSuffix(".py"), (script as NSString).deletingLastPathComponent == directory,
           FileManager.default.fileExists(atPath: launcher) {
            process.arguments = [launcher, String(name.dropLast(3))] + args
        } else {
            process.arguments = [script] + args
        }
        // an app started from Finder gets a thin environment: the script needs USER
        // (the Keychain account name) and a PATH that finds `claude` and `orca`
        var env = ProcessInfo.processInfo.environment
        env["USER"] = NSUserName()
        env["HOME"] = NSHomeDirectory()
        env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = env
        return process
    }

    static func run(_ args: [String], script: String = accounts) async -> CLIResult {
        await run(process(args, script: script))
    }

    /// Runs the script off the main actor. `started` gets its pid, so a sign-in can be cancelled.
    @concurrent
    static func run(_ process: sending Process, started: (@Sendable (Int32) -> Void)? = nil) async -> CLIResult {
        runBlocking(process, started: started)
    }

    nonisolated static func runBlocking(_ process: Process, started: (@Sendable (Int32) -> Void)? = nil) -> CLIResult {
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        do {
            try process.run()
        } catch {
            return CLIResult(status: -1, stdout: "", stderr: error.localizedDescription)
        }
        started?(process.processIdentifier)
        // read both pipes to the end at the same time, or a full pipe stalls the script
        let errData = DataBox()
        let errHandle = err.fileHandleForReading
        let group = DispatchGroup()
        DispatchQueue.global().async(group: group) { errData.value = errHandle.readDataToEndOfFile() }
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        group.wait()
        process.waitUntilExit()
        return CLIResult(
            status: process.terminationStatus,
            stdout: String(decoding: outData, as: UTF8.self),
            stderr: String(decoding: errData.value, as: UTF8.self))
    }
}

/// Written by one reader thread, read after `group.wait()`: the group orders the two.
private nonisolated final class DataBox: @unchecked Sendable {
    var value = Data()
}
