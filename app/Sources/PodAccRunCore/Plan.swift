// What pod-acc-run starts: the launchd agents Pod registers through SMAppService name a program
// inside Pod.app (BundleProgram), never a path in HOME, so this small front finds the account's
// claude-acc state and hands the job to the same interpreter and acc.py the legacy agents run.
import Foundation

public enum PodAccRun {
    /// Where the job goes: the interpreter setup.sh linked, acc.py with the job's words, and the log
    /// both outputs append to (the legacy agents' StandardOutPath names, in $STATE).
    public struct Plan: Equatable, Sendable {
        public let state: String
        public let log: String
        public let argv: [String]
    }

    public enum Failure: Error, Equatable {
        /// no job words, or a log name that is not a plain file name
        case usage(String)
        /// $STATE without the interpreter or acc.py: Pod hasn't run the payload's setup.sh yet
        case notInstalled(String)

        /// sysexits: EX_USAGE, and EX_CONFIG for launchd's log (the agent retries on its own schedule)
        public var exitCode: Int32 {
            switch self {
            case .usage: 64
            case .notInstalled: 78
            }
        }

        public var message: String {
            switch self {
            case .usage(let text), .notInstalled(let text): text
            }
        }
    }

    public static let usage = "usage: pod-acc-run [--log NAME] <acc.py job> [args...]"
    static let defaultLog = "pod-acc-run.log"

    /// The plan for `pod-acc-run [--log NAME] job args...` in the account whose home is `home`.
    /// `exists` answers for a path; the real one is access(2), tests pass a set.
    public static func plan(home: String, args: [String], exists: (String) -> Bool) -> Result<Plan, Failure> {
        var rest = args[...]
        var log = defaultLog
        if rest.first == "--log" {
            rest = rest.dropFirst()
            guard let name = rest.first, !name.isEmpty, !name.contains("/"), name != ".", name != ".." else {
                return .failure(.usage(usage))
            }
            log = name
            rest = rest.dropFirst()
        }
        guard let job = rest.first, !job.isEmpty, !job.hasPrefix("-") else {
            return .failure(.usage(usage))
        }
        let state = (home.hasSuffix("/") ? String(home.dropLast()) : home) + "/.local/share/claude-acc"
        let python = state + "/python", launcher = state + "/acc.py"
        guard exists(python), exists(launcher) else {
            return .failure(.notInstalled("pod-acc-run: claude-acc is not installed in \(state) yet (no python or acc.py)"))
        }
        return .success(Plan(state: state, log: state + "/" + log, argv: [python, launcher] + rest))
    }
}
