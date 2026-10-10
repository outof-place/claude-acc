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
    let first = try #require(policy.next(actable: [shutdown, stop], brakeDue: false, now: 100))
    #expect(first.plan === shutdown && !first.brake)
    policy.record(first, acted: false, brakeGap: 20, now: 100)
    let second = try #require(policy.next(actable: [shutdown, stop], brakeDue: true, now: 102))
    #expect(second.plan === stop && second.brake)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 102) == nil)
    #expect(policy.next(actable: [shutdown], brakeDue: true, now: 102)?.brake == true)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 160.5)?.plan === shutdown)
}

@Test("a declined plan that acted is not held, and one Python always runs is tried on the next tick")
func noHoldWithoutReason() {
    var policy = HandoverPolicy()
    let shutdown = plan("shutdown", "simulator_cap"), stop = plan("stop", "pressure")
    policy.record(.init(plan: shutdown, brake: false), acted: true, brakeGap: 20, now: 0)
    policy.record(.init(plan: stop, brake: false), acted: false, brakeGap: 20, now: 0)
    #expect(policy.next(actable: [shutdown], brakeDue: false, now: 2)?.plan === shutdown)
    #expect(policy.next(actable: [stop], brakeDue: false, now: 2)?.plan === stop)
    #expect(policy.heldUntil.isEmpty)
}

@Test("after a brake handover the brake waits its gap, whatever Python did")
func brakeBackoff() throws {
    var policy = HandoverPolicy()
    let h = try #require(policy.next(actable: [], brakeDue: true, now: 0))
    #expect(h.brake && h.plan == nil)
    policy.record(h, acted: false, brakeGap: 5, now: 0)
    #expect(policy.next(actable: [], brakeDue: true, now: 2) == nil)
    #expect(policy.next(actable: [], brakeDue: true, now: 5)?.brake == true)
}

private final class Calls: @unchecked Sendable {
    var once = 0
    var caps = 0
}

@Test("engine: a runaway's brake goes to Python once per gap, caps only when enforcing, a wrong config type hands every tick over")
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

    #expect(Files.writeAtomic(config, #"{"mode": "observe", "runtimes": "node", "notify": false}"#))
    now += 2
    engine.benchTick()
    now += 2
    engine.benchTick()
    #expect(calls.once == 4)
}
