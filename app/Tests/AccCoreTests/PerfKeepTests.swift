import Darwin
import Foundation
import Synchronization
import Testing
@testable import AccCore

// perf keep on events: which run each change starts, with a runner in place of Python's.

private final class Runs: @unchecked Sendable {
    let lock = NSLock()
    var argv: [[String]] = []
    var version = 1

    func add(_ a: [String]) {
        lock.lock()
        argv.append(a)
        lock.unlock()
    }

    var all: [[String]] {
        lock.lock()
        defer { lock.unlock() }
        return argv
    }

    /// Each run's `--only` names, "(all)" for a full pass.
    var onlys: [String] { all.map { a in a.firstIndex(of: "--only").map { a[$0 + 1] } ?? "(all)" } }
}

private func tempDir(_ name: String) throws -> String {
    var template = Array("/tmp/acc-cored-perf-\(name)-XXXXXX".utf8CString)
    return try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
}

private func waitFor(_ seconds: Double = 5, _ done: () -> Bool) async throws {
    let end = Kernel.monotonic() + seconds
    while !done(), Kernel.monotonic() < end { try await Task.sleep(nanoseconds: 20_000_000) }
}

private func spec(_ dir: String, _ tweaks: [(String, String, [String], Bool)], periodic: Double = 1800,
                  every: [String: Double] = [:], retry: [String: Double] = [:]) -> PyObject {
    PyObject([
        ("v", 1), ("periodic_s", .double(periodic)), ("config", .array([.string(dir + "/perf-state.json")])),
        ("retry", .object(PyObject(retry.map { ($0.key, PyJSON.double($0.value)) }))),
        ("tweaks", .array(tweaks.map { t in
            var fields: [(String, PyJSON)] = [("name", .string(t.0)), ("class", .string(t.1)), ("paths", .array(t.2.map { .string($0) })),
                                               ("network", .bool(t.3))]
            if let s = every[t.0] { fields.append(("every_s", .double(s))) }
            return .object(PyObject(fields))
        })),
    ])
}

private func engine(_ dir: String, _ runs: Runs, _ configure: (inout PerfKeepEngine.Options) -> Void = { _ in }) -> PerfKeepEngine {
    var options = PerfKeepEngine.Options(home: dir, python: ["/py", "/acc.py"])
    options.debounce = 0.1
    options.networkSettle = 0.1
    configure(&options)
    let e = PerfKeepEngine(options)
    e.runner = { argv in
        runs.add(argv)
        return 0
    }
    return e
}

@Test("a write to a tweak's file runs just that tweak, after the full pass at start; a periodic tweak never on an event")
func perfKeepRunsTheTweakOnItsFile() async throws {
    let dir = try tempDir("one")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let settings = dir + "/settings.json", devguard = dir + "/devguard.json"
    #expect(Files.writeAtomic(settings, "{}"))
    #expect(Files.writeAtomic(devguard, "{}"))
    let runs = Runs()
    let e = engine(dir, runs)
    e.specReader = {
        spec(dir, [("claude-limits", "perf", [settings], false), ("devguard-budget", "safety", [devguard], false),
                   ("bg-helpers", "periodic", [], false)])
    }
    e.start()
    try await waitFor { runs.all.count == 1 }
    #expect(runs.all.first == ["/usr/sbin/taskpolicy", "-c", "utility", "/py", "/acc.py", "perf", "keep"])
    #expect(Files.writeAtomic(settings, #"{"x": 1}"#))
    try await waitFor { runs.all.count == 2 }
    #expect(Files.writeAtomic(devguard, #"{"y": 1}"#))
    try await waitFor { runs.all.count == 3 }
    try await Task.sleep(nanoseconds: 600_000_000)
    #expect(runs.onlys == ["(all)", "claude-limits", "devguard-budget"])
    #expect(e.runs.load(ordering: .relaxed) == 3)
    e.stop()
}

@Test("a run's own write gets one confirming run, which writes nothing, and that's all")
func perfKeepConfirmsItsOwnWrite() async throws {
    let dir = try tempDir("own")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let settings = dir + "/settings.json"
    #expect(Files.writeAtomic(settings, "{}"))
    let runs = Runs()
    let e = engine(dir, runs)
    e.specReader = { spec(dir, [("claude-limits", "perf", [settings], false)]) }
    e.runner = { argv in
        runs.add(argv)
        // the repair Python makes after the revert below, as the second run
        if runs.all.count == 2 { _ = Files.writeAtomic(settings, #"{"repaired": true}"#) }
        return 0
    }
    e.start()
    try await waitFor { runs.all.count == 1 }
    #expect(Files.writeAtomic(settings, #"{"reverted": true}"#))
    try await waitFor { runs.all.count == 3 }
    try await Task.sleep(nanoseconds: 1_000_000_000)
    #expect(runs.onlys == ["(all)", "claude-limits", "claude-limits"])
    e.stop()
}

@Test("a stream of events doesn't push the run back: it starts while the stream goes on")
func perfKeepAStreamDoesNotWait() async throws {
    let dir = try tempDir("stream")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let settings = dir + "/settings.json"
    #expect(Files.writeAtomic(settings, "{}"))
    let runs = Runs()
    let e = engine(dir, runs)  // debounce 0.1 s
    e.specReader = { spec(dir, [("claude-limits", "perf", [settings], false)]) }
    e.start()
    try await waitFor { runs.all.count == 1 }
    // a write every 30 ms for 1.5 s, closer together than the debounce
    var ranDuring = false
    for i in 0..<50 {
        #expect(Files.writeAtomic(settings, "{\"n\": \(i)}"))
        try await Task.sleep(nanoseconds: 30_000_000)
        if runs.all.count > 1 { ranDuring = true }
    }
    #expect(ranDuring)
    e.stop()
}

@Test("a change to the config reloads the spec without a run; the new tweak's file then counts")
func perfKeepReloadsItsSpec() async throws {
    let dir = try tempDir("spec")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let settings = dir + "/settings.json", rg = dir + "/ripgreprc"
    #expect(Files.writeAtomic(settings, "{}"))
    #expect(Files.writeAtomic(rg, "--threads=4"))
    let runs = Runs()
    let e = engine(dir, runs)
    e.specReader = {
        runs.version == 1
            ? spec(dir, [("claude-limits", "perf", [settings], false)])
            : spec(dir, [("claude-limits", "perf", [settings], false), ("rg-threads", "perf", [rg], false)])
    }
    e.start()
    try await waitFor { runs.all.count == 1 }
    #expect(Files.writeAtomic(rg, "--threads=8"))  // not watched yet
    try await Task.sleep(nanoseconds: 500_000_000)
    runs.version = 2
    #expect(Files.writeAtomic(dir + "/perf-state.json", #"{"applied": {"rg-threads": {}}}"#))
    try await waitFor { e.queue.sync { e.tweaks.count == 2 } }
    try await Task.sleep(nanoseconds: 300_000_000)
    #expect(runs.all.count == 1)  // a reload alone runs nothing
    #expect(Files.writeAtomic(rg, "--threads=2"))
    try await waitFor { runs.all.count == 2 }
    #expect(runs.onlys == ["(all)", "rg-threads"])
    e.stop()
}

@Test("a network change runs the tweaks that depend on it; the periodic pass runs everything")
func perfKeepNetworkAndPeriodic() async throws {
    let dir = try tempDir("net")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let runs = Runs()
    let e = engine(dir, runs) { $0.minPeriodic = 0.3 }
    e.specReader = { spec(dir, [("tether-profile", "perf", [], true), ("link", "perf", [], true), ("claude-ui", "perf", [], false)], periodic: 1800) }
    e.start()
    try await waitFor { runs.all.count == 1 }
    e.queue.sync { e.networkChanged() }
    try await waitFor { runs.all.count == 2 }
    #expect(runs.onlys == ["(all)", "link,tether-profile"])
    e.stop()

    let periodic = Runs()
    let p = engine(dir, periodic) { $0.minPeriodic = 0.3 }
    p.specReader = { spec(dir, [("claude-ui", "perf", [], false)], periodic: 0.3) }
    p.start()
    try await waitFor { periodic.all.count >= 3 }
    p.stop()
    #expect(Set(periodic.onlys) == ["(all)"])
    #expect(p.queue.sync { p.periodicTimer } == nil)  // stop cancels it
}

@Test("a tweak with its own interval (docker-idle's samples) runs alone that often; stop cancels it")
func perfKeepTweakInterval() async throws {
    let dir = try tempDir("every")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let runs = Runs()
    let e = engine(dir, runs) { $0.minPeriodic = 0.2 }
    e.specReader = { spec(dir, [("docker-idle", "periodic", [], false), ("claude-ui", "perf", [], false)], every: ["docker-idle": 0.3]) }
    e.start()
    try await waitFor { runs.all.count >= 3 }
    e.stop()
    #expect(runs.onlys.first == "(all)")
    #expect(Set(runs.onlys.dropFirst()) == ["docker-idle"])
    #expect(e.queue.sync { e.everyTimer } == nil)
}

@Test("a safety tweak waiting in a fight runs at its turn, from one one-shot timer that's armed only then")
func perfKeepRetryInAFight() async throws {
    let dir = try tempDir("retry")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let runs = Runs()
    let e = engine(dir, runs)
    let tweaks = [("devguard-budget", "safety", [dir + "/devguard.json"], false)]
    let turn = Kernel.wall() + 0.4
    e.specReader = { spec(dir, tweaks, retry: ["devguard-budget": turn]) }
    e.start()
    try await waitFor { runs.all.count >= 2 }
    #expect(runs.onlys == ["(all)", "devguard-budget"])
    #expect(Kernel.wall() >= turn)
    #expect(e.queue.sync { e.retryTimer } == nil)  // fired once, not a tick
    // no tweak waiting: no timer at all
    e.specReader = { spec(dir, tweaks) }
    e.queue.sync { e.loadSpec() }
    #expect(e.queue.sync { e.retryTimer } == nil)
    try await Task.sleep(nanoseconds: 600_000_000)
    #expect(runs.all.count == 2)
    e.stop()
}

@Test("without a spec (a perf.py before keep-spec), the full pass runs at launchd's interval")
func perfKeepWithoutASpec() async throws {
    let dir = try tempDir("nospec")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let runs = Runs()
    let e = engine(dir, runs) {
        $0.minPeriodic = 0.2
        $0.fallbackPeriodic = 0.3
    }
    e.specReader = { nil }
    e.start()
    try await waitFor { runs.all.count >= 3 }
    e.stop()
    #expect(Set(runs.onlys) == ["(all)"])
    #expect(e.queue.sync { e.periodic } == 0.3)
}

@Test("the path watch: only a watched path whose stamp moved, a folder made later, a replacement by rename")
func pathWatchReportsOnlyItsPaths() async throws {
    let dir = try tempDir("watch")
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let queue = DispatchQueue(label: "path-watch-test")
    let watch = PathWatch(queue: queue)
    let seen = Runs()
    watch.changed = { seen.add($0) }
    let later = dir + "/sub/x.json", here = dir + "/here.json"
    #expect(Files.writeAtomic(here, "1"))
    queue.sync { watch.set([later, here]) }
    #expect(Files.writeAtomic(dir + "/other.json", "1"))  // beside a watched file
    try await Task.sleep(nanoseconds: 300_000_000)
    #expect(seen.all.isEmpty)
    Files.mkdirs(dir + "/sub")
    #expect(Files.writeAtomic(later, "1"))
    try await waitFor { seen.all.contains([later]) }
    #expect(seen.all.contains([later]))  // the folder made later, through its parent's watch
    #expect(Files.writeAtomic(here, "2"))  // temp file, then rename over it
    try await waitFor { seen.all.contains([here]) }
    #expect(seen.all.contains([here]))
    let before = seen.all.count
    #expect(Files.writeAtomic(here, "3"))  // the watch follows the new inode
    try await waitFor { seen.all.count > before }
    #expect(seen.all.last == [here])
    // an in-place write after the replace: no folder event, only the watch on the new inode sees it
    let inPlace = seen.all.count
    let fd = open(here, O_WRONLY | O_APPEND)
    #expect(fd >= 0)
    #expect("4".withCString { write(fd, $0, 1) } == 1)
    close(fd)
    try await waitFor { seen.all.count > inPlace }
    #expect(seen.all.count > inPlace)
    queue.sync { watch.stop() }
    #expect(queue.sync { watch.fileSources.isEmpty && watch.dirSources.isEmpty && watch.parentSources.isEmpty })
}
