// claude-acc-hook: the native front of the `devguard.py admit` PreToolUse hook, and with
// `pause <mode>` of the limit pause hooks.
//
// The hook runs before every Bash command of every agent on the Mac. Most commands neither
// start a dev server nor bring Go or JS work for the scheduler, and for those the answer is
// "nothing to say": this binary gives it in about a millisecond, where starting Python alone
// takes 25-40 ms (and hundreds under load). A command where one of the words stands as a whole
// word goes to `devguard.py admit` with the same bytes on stdin, so every decision stays in
// Python. The words and the gate come from `devguard.py words`, written to hook-words.json at
// install: one source for both fronts.
//
// Anything unexpected (no words file, no interpreter) hands the event to Python as well;
// only an event that isn't JSON ends here, exactly as the Python fast path ends it.
import Darwin
import Foundation

// HOME first, like the Python scripts' expanduser: tests point it at a scratch folder
let state = (ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()) + "/.local/share/claude-acc"

// `pause <mode>`: the limit pause hooks of hook.py, which Claude Code runs without a shell
// (exec form), after every tool call of every session. Outside a pause that is two file
// checks and the event drained; during one, hook.py gets the event untouched on stdin,
// through acc.py on the managed interpreter like claude-acc-pause, or without them through
// /usr/bin/python3. The same checks as the shell guard hook.py installs when this binary is
// missing.
if CommandLine.arguments.count > 2, CommandLine.arguments[1] == "pause" {
    let override = ProcessInfo.processInfo.environment["CLAUDE_ACC_PAUSE_FILE"] ?? ""
    let pause = override.isEmpty ? state + "/pause.json" : override
    let script = state + "/hook.py"
    if access(pause, F_OK) == 0, access(script, F_OK) == 0 {
        let python = state + "/python", launcher = state + "/acc.py"
        let args = access(python, X_OK) == 0 && access(launcher, F_OK) == 0
            ? [python, launcher, "hook", CommandLine.arguments[2]]
            : ["/usr/bin/python3", script, CommandLine.arguments[2]]
        var argv = args.map { strdup($0) } + [nil]
        execv(args[0], &argv)
    }
    // the shell's `cat >/dev/null`: Claude Code's write of the event never meets a closed pipe
    _ = FileHandle.standardInput.readDataToEndOfFile()
    exit(0)
}

let event = FileHandle.standardInput.readDataToEndOfFile()

/// The managed interpreter setup.sh links, or the system one.
func interpreter() -> String {
    let linked = state + "/python"
    return access(linked, X_OK) == 0 ? linked : "/usr/bin/python3"
}

/// `devguard.py admit` with the event on stdin, through acc.py (bytecode from the cache) when
/// it is there. An unlinked temp file holds the event, so one of any size is there in full
/// before Python starts reading.
func handOver() -> Never {
    var template = Array((NSTemporaryDirectory() + "claude-acc-hook.XXXXXX").utf8CString)
    let fd = mkstemp(&template)
    guard fd >= 0 else { exit(0) }
    unlink(template)
    let written = event.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    guard written == event.count, lseek(fd, 0, SEEK_SET) == 0, dup2(fd, STDIN_FILENO) >= 0 else { exit(0) }
    close(fd)
    let python = interpreter()
    // `claude-acc-hook codex`: the same hook for Codex, whose rewrite needs an "allow" next to it
    let codex = CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "codex"
    let launcher = state + "/acc.py"
    let admit = access(launcher, F_OK) == 0
        ? [python, launcher, "devguard", "admit"]
        : [python, state + "/devguard.py", "admit"]
    let args = admit + (codex ? ["--codex"] : [])
    var argv = args.map { strdup($0) } + [nil]
    execv(python, &argv)
    exit(0)  // the hook never blocks an agent over its own failure
}

guard let data = FileManager.default.contents(atPath: state + "/hook-words.json"),
      let words = try? JSONSerialization.jsonObject(with: data) as? [String: [String]],
      let dev = words["dev"], let sched = words["sched"], !dev.isEmpty, !sched.isEmpty
else { handOver() }

guard let parsed = try? JSONSerialization.jsonObject(with: event) as? [String: Any] else { exit(0) }
let input = parsed["tool_input"] as? [String: Any]
// Codex sends a shell command as a string, as an argv array, or as `cmd` (exec_command)
let command = input?["command"] as? String ?? input?["cmd"] as? String
    ?? (input?["command"] as? [String])?.joined(separator: " ") ?? ""

// Python's `word in command` compares code points; on UTF-8 bytes that is a byte search
let bytes = Array(command.utf8)
func contains(_ word: String) -> Bool {
    let needle = Array(word.utf8)
    guard !needle.isEmpty, needle.count <= bytes.count else { return needle.isEmpty }
    return bytes.withUnsafeBytes { haystack in
        needle.withUnsafeBytes { memmem(haystack.baseAddress, haystack.count, $0.baseAddress, $0.count) != nil }
    }
}

// whole words only (HOOK_GATE in devguard.py); a words file from before the gate keeps substrings
if let pattern = words["gate"]?.first, let gate = try? NSRegularExpression(pattern: pattern) {
    if gate.firstMatch(in: command, range: NSRange(command.startIndex..., in: command)) != nil { handOver() }
    exit(0)
}
if (dev + sched).contains(where: contains) { handOver() }
exit(0)
