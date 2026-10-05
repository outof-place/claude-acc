// claude-acc-hook: the native front of the `devguard.py admit` PreToolUse hook.
//
// The hook runs before every Bash command of every agent on the Mac. Most commands neither
// start a dev server nor run Go, and for those the answer is "nothing to say": this binary
// gives it in about a millisecond, where starting Python alone takes 25-40 ms (and hundreds
// under load). A command with one of the words goes to `devguard.py admit` with the same
// bytes on stdin, so every decision stays in Python. The words come from
// `devguard.py words`, written to hook-words.json at install: one source for both lists.
//
// Anything unexpected (no words file, no interpreter) hands the event to Python as well;
// only an event that isn't JSON ends here, exactly as the Python fast path ends it.
import Darwin
import Foundation

// HOME first, like the Python scripts' expanduser: tests point it at a scratch folder
let state = (ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()) + "/.local/share/claude-acc"
let event = FileHandle.standardInput.readDataToEndOfFile()

/// The managed interpreter setup.sh links, or the system one.
func interpreter() -> String {
    let linked = state + "/python"
    return access(linked, X_OK) == 0 ? linked : "/usr/bin/python3"
}

/// `devguard.py admit` with the event on stdin. An unlinked temp file holds it, so an event
/// of any size is there in full before Python starts reading.
func handOver() -> Never {
    var template = Array((NSTemporaryDirectory() + "claude-acc-hook.XXXXXX").utf8CString)
    let fd = mkstemp(&template)
    guard fd >= 0 else { exit(0) }
    unlink(template)
    let written = event.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    guard written == event.count, lseek(fd, 0, SEEK_SET) == 0, dup2(fd, STDIN_FILENO) >= 0 else { exit(0) }
    close(fd)
    let python = interpreter()
    let args = [python, state + "/devguard.py", "admit"]
    var argv = args.map { strdup($0) } + [nil]
    execv(python, &argv)
    exit(0)  // the hook never blocks an agent over its own failure
}

guard let data = FileManager.default.contents(atPath: state + "/hook-words.json"),
      let words = try? JSONSerialization.jsonObject(with: data) as? [String: [String]],
      let dev = words["dev"], let go = words["go"], !dev.isEmpty, !go.isEmpty
else { handOver() }

guard let parsed = try? JSONSerialization.jsonObject(with: event) as? [String: Any] else { exit(0) }
let command = (parsed["tool_input"] as? [String: Any])?["command"] as? String ?? ""

// Python's `word in command` compares code points; on UTF-8 bytes that is a byte search
let bytes = Array(command.utf8)
func contains(_ word: String) -> Bool {
    let needle = Array(word.utf8)
    guard !needle.isEmpty, needle.count <= bytes.count else { return needle.isEmpty }
    return bytes.withUnsafeBytes { haystack in
        needle.withUnsafeBytes { memmem(haystack.baseAddress, haystack.count, $0.baseAddress, $0.count) != nil }
    }
}

if (dev + go).contains(where: contains) { handOver() }
exit(0)
