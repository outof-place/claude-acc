import Darwin
import Foundation
import Testing
@testable import AccCore

// Which tick goes to Python (`acc.py devguard once`) and the waits after a handover.

private func plan(_ action: String, _ code: String) -> Plan {
    Plan(.simulators(footprint: 0, count: 0), action, 50, "test", code)
}

@Test("a plan Python declined waits; the plans under it and a due brake still go")
func heldPlanDoesNotMaskOthers() throws {
    var policy = HandoverPolicy()
    let shutdown = plan("shutdown", "simulator_idle"), stop = plan("stop", "idle")
    let first = try #require(policy.next(actable: [shutdown, stop], brakeDue: false, now: 100, brakeGap: 20))
    #expect(first.plan === shutdown && !first.brake)
    policy.record(first, acted: false, now: 100)
    let second = try #require(policy.next(actable: [shutdown, stop], brakeDue: true, now: 102, brakeGap: 20))
    #expect(second.plan === stop && second.brake)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 102, brakeGap: 20) == nil)
    #expect(policy.next(actable: [shutdown], brakeDue: true, now: 102, brakeGap: 20)?.brake == true)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 160.5, brakeGap: 20)?.plan === shutdown)
}

@Test("a declined plan that acted is not held, and one Python always runs is tried on the next tick")
func noHoldWithoutReason() {
    var policy = HandoverPolicy()
    let shutdown = plan("shutdown", "simulator_cap"), stop = plan("stop", "pressure")
    policy.record(.init(plan: shutdown, brake: false), acted: true, now: 0)
    policy.record(.init(plan: stop, brake: false), acted: false, now: 0)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 2, brakeGap: 20)?.plan === shutdown)
    #expect(policy.next(actable: [stop], brakeDue: false, now: 2, brakeGap: 20)?.plan === stop)
    #expect(policy.heldUntil.isEmpty)
}

@Test("after a brake handover the brake waits the gap of the stage it is at now, whatever Python did")
func brakeBackoff() throws {
    var policy = HandoverPolicy()
    let h = try #require(policy.next(actable: [], brakeDue: true, now: 0, brakeGap: 20))
    #expect(h.brake && h.plan == nil)
    policy.record(h, acted: false, now: 0)
    #expect(policy.next(actable: [], brakeDue: true, now: 2, brakeGap: 20) == nil)
    // pressure climbs to stage 3 (5 s): the wait shortens at once
    #expect(policy.next(actable: [], brakeDue: true, now: 6, brakeGap: 5)?.brake == true)
    #expect(policy.next(actable: [], brakeDue: true, now: 20, brakeGap: 20)?.brake == true)
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
    w.now += 6
    w.compressed = 30  // 62%: stage 3, whose gap is 5 s
    engine.benchTick()
    #expect(w.once == 2)
    w.now += 2
    engine.benchTick()
    #expect(w.once == 2)
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
