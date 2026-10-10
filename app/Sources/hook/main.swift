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
// only an event that isn't JSON ends here, exactly as the Python fast path ends it. In Pod,
// whose agent codes.pod.app.acc.admit runs `devguard.py admitd`, a handed-over event goes to
// that warm process over $STATE/admit.sock first (askAdmitd), and to the exec only without it.
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

func millis() -> Int64 { Int64(clock_gettime_nsec_np(CLOCK_MONOTONIC) / 1_000_000) }

/// Waits until `fd` is ready for `events` or the monotonic deadline passes.
func ready(_ fd: Int32, _ events: Int32, until end: Int64) -> Bool {
    while true {
        let left = end - millis()
        if left <= 0 { return false }
        var p = pollfd(fd: fd, events: Int16(events), revents: 0)
        let r = poll(&p, 1, Int32(left))
        if r < 0 && errno == EINTR { continue }
        return r > 0
    }
}

func writeAll(_ fd: Int32, _ data: Data, until end: Int64) -> Bool {
    data.withUnsafeBytes { raw -> Bool in
        var off = 0
        while off < raw.count {
            let w = write(fd, raw.baseAddress! + off, raw.count - off)
            if w > 0 { off += w; continue }
            if w < 0 && (errno == EAGAIN || errno == EINTR) && ready(fd, POLLOUT, until: end) { continue }
            return false
        }
        return true
    }
}

func readExactly(_ fd: Int32, _ n: Int, until end: Int64) -> Data? {
    guard n > 0 else { return Data() }
    var buf = [UInt8](repeating: 0, count: n)
    var got = 0
    while got < n {
        let r = buf.withUnsafeMutableBytes { read(fd, $0.baseAddress! + got, n - got) }
        if r > 0 { got += r; continue }
        if r < 0 && (errno == EAGAIN || errno == EINTR) && ready(fd, POLLIN, until: end) { continue }
        return nil  // EOF, an error or the deadline
    }
    return Data(buf)
}

/// The warm admit (`devguard.py admitd` on $STATE/admit.sock, Pod's agent codes.pod.app.acc.admit):
/// the answer the exec below would print, without starting Python. Protocol v1 (admitd.py): a u32
/// big-endian length and JSON both ways, with the hook's whole environment and working directory.
/// Anything else (no daemon, a foreign peer, no answer within 3 s, another version, an event that
/// isn't UTF-8) returns and the exec runs as before; admit writes nothing, so nothing runs twice.
/// CLAUDE_ACC_ADMIT_SOCK=0 skips it (pod-hookd sets it after its own try failed).
func askAdmitd(codex: Bool) {
    let env = ProcessInfo.processInfo.environment
    guard env["CLAUDE_ACC_ADMIT_SOCK"] != "0", let text = String(data: event, encoding: .utf8) else { return }
    var addr = sockaddr_un()
    let path = Array((state + "/admit.sock").utf8)
    guard path.count < MemoryLayout.size(ofValue: addr.sun_path) else { return }
    addr.sun_family = sa_family_t(AF_UNIX)
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: path)
        raw[path.count] = 0
    }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return }
    defer { close(fd) }
    var on: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
    _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
    let connected = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    if connected != 0 {
        guard errno == EINPROGRESS, ready(fd, POLLOUT, until: millis() + 50) else { return }
        var failure: Int32 = 0
        var size = socklen_t(MemoryLayout<Int32>.size)
        guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &failure, &size) == 0, failure == 0 else { return }
    }
    var uid = uid_t(0), gid = gid_t(0)
    guard getpeereid(fd, &uid, &gid) == 0, uid == geteuid() else { return }
    let request: [String: Any] = ["v": 1, "argv": codex ? ["admit", "--codex"] : ["admit"], "event": text,
                                  "cwd": FileManager.default.currentDirectoryPath, "env": env]
    guard let body = try? JSONSerialization.data(withJSONObject: request), body.count <= 8 << 20 else { return }
    var length = UInt32(body.count).bigEndian
    let end = millis() + 3000
    guard writeAll(fd, Data(bytes: &length, count: 4) + body, until: end),
          let head = readExactly(fd, 4, until: end) else { return }
    let n = head.reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
    guard n <= 8 << 20, let data = readExactly(fd, Int(n), until: end),
          let answer = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          answer["v"] as? Int == 1, let out = answer["stdout"] as? String, let code = answer["code"] as? Int
    else { return }
    FileHandle.standardError.write(Data((answer["stderr"] as? String ?? "").utf8))
    FileHandle.standardOutput.write(Data(out.utf8))
    exit(Int32(truncatingIfNeeded: code))
}

/// `devguard.py admit` with the event on stdin, through acc.py (bytecode from the cache) when
/// it is there. An unlinked temp file holds the event, so one of any size is there in full
/// before Python starts reading.
func handOver() -> Never {
    // `claude-acc-hook codex`: the same hook for Codex, whose rewrite needs an "allow" next to it
    let codex = CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "codex"
    askAdmitd(codex: codex)
    var template = Array((NSTemporaryDirectory() + "claude-acc-hook.XXXXXX").utf8CString)
    let fd = mkstemp(&template)
    guard fd >= 0 else { exit(0) }
    unlink(template)
    let written = event.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
    guard written == event.count, lseek(fd, 0, SEEK_SET) == 0, dup2(fd, STDIN_FILENO) >= 0 else { exit(0) }
    close(fd)
    let python = interpreter()
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
