import Darwin
import Foundation
import Testing
@testable import AccCore

// Which tick goes to Python (`acc.py devguard once`) and the waits after a handover.

// GuardEngine.init and loop() set GuardLog's and GuardContext's statics: in parallel these tests raced
// (SIGSEGV in GuardLog.hold, 3 in ~25 runs), so they run one at a time.
@Suite("the guard engine", .serialized)
struct GuardEngineTests {

private func plan(_ action: String, _ code: String) -> Plan {
    Plan(.simulators(footprint: 0, count: 0), action, 50, "test", code)
}

@Test("a plan Python declined waits; the plans under it and a due brake still go")
func heldPlanDoesNotMaskOthers() throws {
    var policy = HandoverPolicy()
    let shutdown = plan("shutdown", "simulator_idle"), stop = plan("stop", "idle")
    let first = try #require(policy.next(actable: [shutdown, stop], brakeDue: false, now: 100, brakeGap: 20, stage: 2))
    #expect(first.plan === shutdown && !first.brake)
    policy.record(first, acted: false, now: 100, stage: 2)
    let second = try #require(policy.next(actable: [shutdown, stop], brakeDue: true, now: 102, brakeGap: 20, stage: 2))
    #expect(second.plan === stop && second.brake)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 102, brakeGap: 20, stage: 2) == nil)
    #expect(policy.next(actable: [shutdown], brakeDue: true, now: 102, brakeGap: 20, stage: 2)?.brake == true)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 160.5, brakeGap: 20, stage: 2)?.plan === shutdown)
}

@Test("a declined plan that acted is not held, and one Python always runs is tried on the next tick")
func noHoldWithoutReason() {
    var policy = HandoverPolicy()
    let shutdown = plan("shutdown", "simulator_cap"), stop = plan("stop", "pressure")
    policy.record(.init(plan: shutdown, brake: false), acted: true, now: 0, stage: 2)
    policy.record(.init(plan: stop, brake: false), acted: false, now: 0, stage: 2)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 2, brakeGap: 20, stage: 2)?.plan === shutdown)
    #expect(policy.next(actable: [stop], brakeDue: false, now: 2, brakeGap: 20, stage: 2)?.plan === stop)
    #expect(policy.heldUntil.isEmpty)
}

@Test("after a brake handover the brake waits the gap of the stage it is at now, whatever Python did")
func brakeBackoff() throws {
    var policy = HandoverPolicy()
    let h = try #require(policy.next(actable: [], brakeDue: true, now: 0, brakeGap: 20, stage: 2))
    #expect(h.brake && h.plan == nil)
    policy.record(h, acted: false, now: 0, stage: 2)
    #expect(policy.next(actable: [], brakeDue: true, now: 2, brakeGap: 20, stage: 2) == nil)
    // pressure climbs to stage 3: a higher stage goes at once (stage 2 found no victim, or brakeDue
    // would still wait for brake_at), and after a stage-3 brake its own 5 s gap holds
    #expect(policy.next(actable: [], brakeDue: true, now: 2, brakeGap: 5, stage: 3)?.brake == true)
    #expect(policy.next(actable: [], brakeDue: true, now: 6, brakeGap: 20, stage: 2) == nil)
    #expect(policy.next(actable: [], brakeDue: true, now: 20, brakeGap: 20, stage: 2)?.brake == true)
    var three = policy
    three.record(.init(plan: nil, brake: true), acted: false, now: 2, stage: 3)
    #expect(three.next(actable: [], brakeDue: true, now: 4, brakeGap: 5, stage: 3) == nil)
    #expect(three.next(actable: [], brakeDue: true, now: 7, brakeGap: 5, stage: 3)?.brake == true)
    // back to stage 2: its own 20 s from the last brake
    #expect(three.next(actable: [], brakeDue: true, now: 10, brakeGap: 20, stage: 2) == nil)
}

@Test("a server with an app open in a Simulator is declinable; a pool shutdown is; a warn and a plain stop aren't")
func declinablePlans() throws {
    let probes = RecordedProbes(PyObject([
        ("now", 1000.0), ("home", "/h"), ("rows", [[100, 1, "node /w/node_modules/.bin/next dev"]]),
        ("usage", ["100": ["footprint": 1, "peak": 1, "cpu": 0.0, "written": 0, "start": 1]]), ("cwd", ["100": "/w"]),
        ("argv", ["100": ["node", "/w/node_modules/.bin/next", "dev"]]),
    ]))
    let table = ProcTable(probes.processes())
    let server = Server(pid: 100, kind: "next", command: table.command(100), tree: [100], probes: probes)
    let unit = Unit(root: 100, servers: [server], table: table, children: [:], probes: probes)
    let stop = Plan(.unit(unit), "stop", 40, "idle", "idle")
    #expect(!declinable(stop))
    unit.idleSimClients = [.init(pid: 200, kind: "simulator", name: "App")]
    #expect(declinable(stop))
    #expect(declinable(Plan(.unit(unit), "recycle", 70, "bloated", "bloated")))
    #expect(!declinable(Plan(.unit(unit), "warn", 70, "bloated", "bloated_unmanaged")))
    #expect(declinable(Plan(.simulators(footprint: 0, count: 0), "shutdown", 45, "cap", "simulator_cap")))
}

private final class Calls: @unchecked Sendable {
    var once = 0
    var caps = 0
}

@Test("engine: a runaway's brake goes to Python once per gap, caps only when enforcing and after a change")
func engineHandovers() throws {
    var template = Array("/tmp/acc-cored-engine-XXXXXX".utf8CString)
    let home = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: home) }
    let stateDir = GuardPaths.state(home)
    Files.mkdirs(stateDir)
    let config = stateDir + "/devguard.json"
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "caps_minutes": 10, "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    GuardLog.notifications = false
    let gb = 1 << 30
    var now = 1_791_000_000.0
    engine.probesFactory = {
        RecordedProbes(PyObject([
            ("now", .double(now)), ("home", .string(home)), ("rows", [[4242, 1, "node /tmp/x/big.js"]]),
            ("usage", ["4242": ["footprint": .int(40 * gb), "peak": .int(40 * gb), "cpu": 1.0, "written": 0, "start": 1]]),
            ("sysctl", ["hw.memsize": .int(48 * gb)]), ("swap", [0, 0]),
        ]))
    }
    let calls = Calls()
    engine.runner = { _ in calls.once += 1; return 0 }
    engine.capsRunner = { calls.caps += 1 }

    engine.benchTick()  // over half the RAM: the brake is due at any stage
    #expect(calls.once == 1 && calls.caps == 1)
    now += 2
    engine.benchTick()  // brake_cooldown_seconds hasn't passed
    #expect(calls.once == 1)
    now += 60
    engine.benchTick()
    #expect(calls.once == 2)

    #expect(Files.writeAtomic(config, #"{"mode": "observe", "caps_minutes": 10, "notify": false}"#))
    now += 700
    engine.benchTick()  // observing: no handover, and the caps (due by now) don't run either
    #expect(calls.once == 2 && calls.caps == 1)

    // a config of the wrong type: Python's own loop takes over (pythonLoopFallback), no handover
    engine.loopStarter = { _ in nil }
    #expect(Files.writeAtomic(config, #"{"mode": "observe", "runtimes": "node", "notify": false}"#))
    now += 2
    engine.benchTick()
    #expect(calls.once == 2)
}


private func pressureWorld(home: String, now: Double, compressedGB: Int) -> RecordedProbes {
    let gb = 1 << 30
    return RecordedProbes(PyObject([
        ("now", .double(now)), ("home", .string(home)), ("rows", [[4242, 1, "/usr/bin/some-daemon"]]),
        ("usage", ["4242": ["footprint": .int(gb), "peak": .int(gb), "cpu": 1.0, "written": 0, "start": 1]]),
        ("sysctl", ["hw.memsize": .int(48 * gb), "vm.compressor_bytes_used": .int(compressedGB * gb)]), ("swap", [0, 0]),
    ]))
}

private func scratchHome(_ name: String) throws -> String {
    var template = Array("/tmp/acc-cored-\(name)-XXXXXX".utf8CString)
    return try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
}

@Test("engine: a stage-2 brake's wait shortens when pressure reaches stage 3")
func brakeEscalation() throws {
    let home = try scratchHome("escalate")
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/devguard.json", #"{"mode": "enforce", "caps_minutes": 0, "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    GuardLog.notifications = false
    final class World: @unchecked Sendable { var now = 1_791_000_000.0; var compressed = 25; var once = 0 }
    let w = World()
    engine.probesFactory = { pressureWorld(home: home, now: w.now, compressedGB: w.compressed) }
    engine.runner = { _ in w.once += 1; return 0 }

    engine.benchTick()  // 25 of 48 GB compressed: stage 2, a brake
    #expect(w.once == 1)
    w.now += 2
    w.compressed = 30  // 62%: stage 3 two seconds later, with no victim at stage 2: Python reaps at once
    engine.benchTick()
    #expect(w.once == 2)
    w.now += 2
    engine.benchTick()  // stage 3's own 5 s gap
    #expect(w.once == 2)
    w.now += 4
    engine.benchTick()
    #expect(w.once == 3)
}

@Test("engine: a config of the wrong type runs Python's own loop, and a fixed one stops it")
func pythonLoopFallback() throws {
    let home = try scratchHome("loop")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let config = GuardPaths.state(home) + "/devguard.json"
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "runtimes": "node", "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    final class Seen: @unchecked Sendable { var argv: [[String]] = [] }
    let seen = Seen()
    engine.loopStarter = { argv in
        seen.argv.append(argv)
        return Spawn.start(["/bin/sleep", "30"])
    }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }

    engine.benchTick()
    let pid = try #require(engine.queue.sync { engine.pythonLoop })
    #expect(seen.argv == [["/nonexistent/python", "devguard", "run"]])
    #expect(kill(pid, 0) == 0)
    engine.benchTick()  // still running: not started twice
    #expect(seen.argv.count == 1)

    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "protect": [3000, ":3001"], "notify": false}"#))
    engine.benchTick()  // ints in protect read natively: Python's loop ends
    #expect(engine.queue.sync { engine.pythonLoop } == nil)
    #expect(kill(pid, 0) != 0)
}

@Test("engine: a host lookup that failed is asked again later, off the guard's queue")
func hostRetry() throws {
    let home = try scratchHome("host")
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(GuardPaths.state(home))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    final class Calls: @unchecked Sendable { var n = 0 }
    let calls = Calls()
    engine.resolver = { calls.n += 1; return nil }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    let settle = {
        for _ in 0..<100 where engine.queue.sync(execute: { engine.resolving }) { usleep(10_000) }
    }

    engine.benchTick()
    settle()
    #expect(calls.n == 1)
    engine.benchTick()  // within the minute: not again
    settle()
    #expect(calls.n == 1)
    engine.queue.sync { engine.hostRetryAt = 0 }
    engine.benchTick()
    settle()
    #expect(calls.n == 2)
}

/// Whether another descriptor can take devguard.lock now (the guard's own fd holds it otherwise).
private func lockFree(_ home: String) -> Bool {
    let fd = open(GuardPaths.state(home) + "/devguard.lock", O_WRONLY | O_CREAT | O_CLOEXEC, 0o644)
    defer { close(fd) }
    guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { return false }
    flock(fd, LOCK_UN)
    return true
}

@Test("engine: Python's loop gets the lock and gives it back; stop() ends the loop before the lock goes")
func pythonLoopLock() throws {
    let home = try scratchHome("looplock")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let config = GuardPaths.state(home) + "/devguard.json"
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "caps_minutes": 0, "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    engine.loopStarter = { _ in Spawn.start(["/bin/sleep", "30"]) }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    #expect(engine.start())
    #expect(!lockFree(home))

    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "runtimes": "node", "caps_minutes": 0, "notify": false}"#))
    engine.benchTick()
    let first = try #require(engine.queue.sync { engine.pythonLoop })
    #expect(lockFree(home))  // Python's loop takes it
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "caps_minutes": 0, "notify": false}"#))
    engine.benchTick()
    #expect(kill(first, 0) != 0 && !lockFree(home))

    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "runtimes": "node", "caps_minutes": 0, "notify": false}"#))
    engine.benchTick()
    let second = try #require(engine.queue.sync { engine.pythonLoop })
    engine.stop()
    #expect(kill(second, 0) != 0)  // no orphan keeps the lock
    #expect(lockFree(home))
}

@Test("engine: a Python loop that ends at once is started again after 10 s, then 20 s; native QoS flags are dropped")
func pythonLoopBackoff() throws {
    let home = try scratchHome("loopwait")
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(GuardPaths.state(home))
    // interval_seconds as a string: mismatched, and Python's loop dies on it (time.sleep("5"))
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/devguard.json", #"{"mode": "enforce", "interval_seconds": "5", "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    final class Starts: @unchecked Sendable { var n = 0 }
    let starts = Starts()
    engine.loopStarter = { _ in
        starts.n += 1
        return Spawn.start(["/usr/bin/true"])
    }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    engine.queue.sync { engine.shaper.background[99_999] = (1, true) }

    engine.benchTick()
    #expect(starts.n == 1)
    #expect(engine.queue.sync { engine.shaper.background.isEmpty })
    #expect(engine.queue.sync { engine.interval(GuardConfig.load(path: GuardPaths.state(home) + "/devguard.json")) } == 5)
    usleep(200_000)
    for _ in 0..<5 { engine.benchTick() }  // it ended: 10 s before the next start, not 0.5 s
    #expect(starts.n == 1)
    let wait = engine.queue.sync { engine.loopRestartAt - Kernel.wall() }
    #expect(wait > 8 && wait <= 10)
    engine.queue.sync { engine.loopRestartAt = 0 }
    engine.benchTick()
    #expect(starts.n == 2)
    usleep(200_000)
    engine.benchTick()
    let longer = engine.queue.sync { engine.loopRestartAt - Kernel.wall() }
    #expect(longer > 18 && longer <= 20)
    engine.stop()
}

@Test("engine: a Python loop that doesn't start backs off like one that ends at once: 10 s, then 20 s")
func pythonLoopStartFails() throws {
    let home = try scratchHome("loopfail")
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/devguard.json", #"{"mode": "enforce", "interval_seconds": "5", "notify": false}"#))
    // a python that starts: only the stub's nil makes this start fail, not Spawn.start's
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/usr/bin/true"]))
    final class Starts: @unchecked Sendable { var n = 0 }
    let starts = Starts()
    engine.loopStarter = { _ in
        starts.n += 1
        return nil
    }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }

    engine.benchTick()
    #expect(starts.n == 1)
    let first = engine.queue.sync { engine.loopRestartAt - Kernel.wall() }
    #expect(first > 8 && first <= 10)
    engine.queue.sync { engine.loopRestartAt = 0 }
    engine.benchTick()
    #expect(starts.n == 2)
    let second = engine.queue.sync { engine.loopRestartAt - Kernel.wall() }
    #expect(second > 18 && second <= 20)
    #expect(engine.queue.sync { engine.pythonLoop == nil })
    engine.stop()
}

@Test("engine: a Python loop that ignores SIGTERM is killed after 10 s with what it started")
func pythonLoopKill() throws {
    let home = try scratchHome("loopkill")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let config = GuardPaths.state(home) + "/devguard.json"
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "runtimes": "node", "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    engine.loopStarter = { _ in Spawn.start(["/bin/sh", "-c", "trap '' TERM; /bin/sleep 60 & wait"]) }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    engine.benchTick()
    let pid = try #require(engine.queue.sync { engine.pythonLoop })
    var grandchild: pid_t = 0
    for _ in 0..<50 where grandchild == 0 {
        usleep(20_000)
        grandchild = Proc.children(of: pid).first ?? 0
    }
    #expect(grandchild > 0)
    #expect(Files.writeAtomic(config, #"{"mode": "enforce", "notify": false}"#))
    let start = Kernel.wall()
    engine.benchTick()
    #expect(Kernel.wall() - start >= 9.5)
    #expect(kill(pid, 0) != 0 && kill(grandchild, 0) != 0)
}

@Test("brake settings of another type hand the guard to Python, as a string interval does")
func brakeTypes() {
    let cfg = { (text: String) -> [String] in
        let path = "/tmp/acc-cored-brake-\(getpid()).json"
        defer { unlink(path) }
        _ = Files.writeAtomic(path, text)
        return GuardConfig.load(path: path).mismatched
    }
    #expect(cfg(#"{"brake": {"emergency_cooldown_seconds": "5"}}"#) == ["brake.emergency_cooldown_seconds"])
    #expect(cfg(#"{"brake": "fast"}"#) == ["brake"])
    #expect(cfg(#"{"brake": {"emergency_cooldown_seconds": 5, "unknown": "x"}}"#).isEmpty)
    #expect(cfg(#"{"interval_seconds": "5"}"#) == ["interval_seconds"])
}

@Test("engine: back from Python's loop (ended, in its backoff) native takes the lock first and starts from Python's state")
func pythonLoopRecovers() throws {
    let home = try scratchHome("recover")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let config = GuardPaths.state(home) + "/devguard.json"
    let statePath = GuardPaths.state(home) + "/devguard-state.json"
    Files.mkdirs(GuardPaths.state(home))
    let good = #"{"mode": "enforce", "interval_seconds": 3600, "caps_minutes": 0, "notify": false}"#
    let odd = #"{"mode": "enforce", "interval_seconds": 3600, "runtimes": "node", "caps_minutes": 0, "notify": false}"#
    #expect(Files.writeAtomic(config, good))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    engine.loopStarter = { _ in Spawn.start(["/usr/bin/true"]) }  // a loop that ends at once
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    #expect(engine.start())
    defer { engine.stop() }

    #expect(Files.writeAtomic(config, odd))
    engine.benchTick()
    usleep(200_000)
    engine.benchTick()  // it ended: the backoff, no loop, the lock still let go
    #expect(engine.queue.sync { engine.pythonLoop == nil && engine.pythonMode && engine.loopShortRuns == 1 })
    #expect(lockFree(home))
    // what Python's loop saved last
    #expect(Files.writeAtomic(statePath, #"{"brake_at": 1790999990.0, "warned": {"x": 1}, "writer": 1, "saved_at": 1790999990.0}"#))
    // what native pinned before Python's loop: forgotten on the way back, as Python restored it
    engine.queue.sync { engine.shaper.background[99_999] = (1, true) }

    // another guard holds the lock meanwhile: native waits, it doesn't tick beside it
    let other = open(GuardPaths.state(home) + "/devguard.lock", O_WRONLY | O_CLOEXEC)
    #expect(flock(other, LOCK_EX | LOCK_NB) == 0)
    #expect(Files.writeAtomic(config, good))
    let ticks = engine.queue.sync { engine.ticks }
    engine.benchTick()
    #expect(engine.queue.sync { engine.ticks } == ticks && engine.queue.sync { engine.pythonMode })
    flock(other, LOCK_UN)
    close(other)

    engine.benchTick()
    #expect(engine.queue.sync { engine.ticks } == ticks + 1)
    #expect(!lockFree(home))
    #expect(engine.queue.sync { !engine.pythonMode && engine.loopShortRuns == 0 })
    #expect(engine.queue.sync { engine.shaper.background[99_999] == nil })
    let saved = Files.json(statePath, fallback: .null)
    #expect(saved["brake_at"]?.double == 1_790_999_990 && saved["warned"]?["x"]?.double == 1)
}

@Test("an interval_seconds too large for a timer's leeway is capped at an hour, a tiny one at half a second")
func intervalCap() {
    let path = "/tmp/acc-cored-interval-\(getpid()).json"
    defer { unlink(path) }
    let engine = GuardEngine(.init(home: "/nonexistent", shadow: true, python: ["/nonexistent/python"]))
    for (raw, want) in [("1e17", 3600.0), ("0.001", 0.5), ("5", 5.0)] {
        _ = Files.writeAtomic(path, "{\"interval_seconds\": \(raw)}")
        #expect(engine.interval(GuardConfig.load(path: path)) == want)
    }
}

@Test("engine: a guard that takes the lock during `devguard once` hangs neither the queue nor stop(); native waits for it")
func handOverLockTaken() throws {
    let home = try scratchHome("handlock")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let stateDir = GuardPaths.state(home)
    Files.mkdirs(stateDir)
    #expect(Files.writeAtomic(stateDir + "/devguard.json", #"{"mode": "enforce", "interval_seconds": 3600, "caps_minutes": 10, "notify": false}"#))
    Files.mkdirs(home + "/caps/p/builds")
    #expect(Files.writeAtomic(stateDir + "/janitor.json", "{\"caps\": [{\"path\": \"\(home)/caps/*/builds\", \"max_gb\": 10}]}"))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    GuardLog.notifications = false
    let gb = 1 << 30
    engine.probesFactory = {
        RecordedProbes(PyObject([
            ("now", .double(1_791_000_000)), ("home", .string(home)), ("rows", [[4242, 1, "node /tmp/x/big.js"]]),
            ("usage", ["4242": ["footprint": .int(40 * gb), "peak": .int(40 * gb), "cpu": 1.0, "written": 0, "start": 1]]),
            ("sysctl", ["hw.memsize": .int(48 * gb)]), ("swap", [0, 0]),
        ]))
    }
    final class Holder: @unchecked Sendable { var fd: Int32 = -1 }
    let holder = Holder()
    engine.runner = { _ in  // `devguard run` from launchd takes the lock while once runs
        holder.fd = open(stateDir + "/devguard.lock", O_WRONLY | O_CLOEXEC)
        _ = flock(holder.fd, LOCK_EX | LOCK_NB)
        return 0
    }
    #expect(engine.start())
    let done = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        engine.benchTick()  // a runaway: the brake goes to Python
        done.signal()
    }
    let finished = done.wait(timeout: .now() + 3) == .success
    #expect(finished, "the tick hung on the lock")
    if !finished {
        flock(holder.fd, LOCK_UN)
        _ = done.wait(timeout: .now() + 5)
    }
    #expect(engine.queue.sync { engine.handovers == 1 && engine.pythonMode })
    // no save without the lock: the caps' due check would have written caps_at
    #expect(Files.json(stateDir + "/devguard-state.json", fallback: .null)["caps_at"] == nil)
    let ticks = engine.queue.sync { engine.ticks }
    engine.benchTick()  // still held: no tick
    #expect(engine.queue.sync { engine.ticks } == ticks)
    flock(holder.fd, LOCK_UN)
    close(holder.fd)
    engine.runner = { _ in 0 }
    engine.benchTick()
    #expect(engine.queue.sync { engine.ticks } == ticks + 1 && !engine.queue.sync { engine.pythonMode })
    #expect(!lockFree(home))
    let start = Kernel.wall()
    engine.stop()
    #expect(Kernel.wall() - start < 2)
}

@Test("engine: the end of Python's loop is logged with its exit code or signal")
func pythonLoopEndLine() throws {
    let home = try scratchHome("endline")
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/devguard.json", #"{"mode": "enforce", "interval_seconds": "5", "notify": false}"#))
    let engine = GuardEngine(.init(home: home, shadow: false, python: ["/nonexistent/python"]))
    final class Next: @unchecked Sendable { var argv = ["/bin/sh", "-c", "exit 3"] }
    let next = Next()
    engine.loopStarter = { _ in Spawn.start(next.argv) }
    engine.probesFactory = { pressureWorld(home: home, now: 1_791_000_000, compressedGB: 1) }
    engine.runner = { _ in 0 }
    engine.benchTick()
    usleep(300_000)
    engine.benchTick()
    next.argv = ["/bin/sh", "-c", "kill -9 $$"]
    engine.queue.sync { engine.loopRestartAt = 0 }
    engine.benchTick()
    usleep(300_000)
    engine.benchTick()
    let log = String(decoding: Files.read(GuardPaths.state(home) + "/devguard.log") ?? [], as: UTF8.self)
    #expect(log.contains("pętla Pythona skończyła się (kod 3)"))
    #expect(log.contains("pętla Pythona skończyła się (sygnał 9)"))
    engine.stop()
}

}
