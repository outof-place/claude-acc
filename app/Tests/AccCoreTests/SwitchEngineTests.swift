import Darwin
import Foundation
import Testing
@testable import AccCore

// accswitch's tick on acc-cored's clock: which 2-minute slot runs it.

private func state(lastTick: Double, quiet: Bool?, at: Double? = nil) -> PyObject {
    var s = PyObject([("last_tick", .double(lastTick))])
    if let quiet {
        s["tick_due"] = .object(PyObject([("quiet", .bool(quiet)), ("at", at.map { .double($0) } ?? .null), ("why", "test")]))
    }
    return s
}

@Test("a slot runs the tick on a changed input, a passed deadline, a busy last tick or the gap; else it skips")
func slotDecisions() {
    let now = 10_000.0
    let decide = { (s: PyObject, changed: [String]) in SwitchEngine.decide(state: s, changed: changed, now: now, maxGap: 480).run }
    #expect(!decide(state(lastTick: now - 120, quiet: true, at: now + 300), []))
    #expect(!decide(state(lastTick: now - 120, quiet: true), []))
    #expect(decide(state(lastTick: now - 120, quiet: true, at: now - 1), []))
    #expect(decide(state(lastTick: now - 120, quiet: false), []))
    #expect(decide(state(lastTick: now - 120, quiet: nil), []))  // a claude-acc without tick_due: every slot
    #expect(decide(state(lastTick: now - 120, quiet: true, at: now + 300), ["keychain"]))
    #expect(decide(state(lastTick: now - 476, quiet: true, at: now + 300), []))
    #expect(!decide(state(lastTick: now - 360, quiet: true, at: now + 300), []))
}

@Test("the API's ISO times, with fractions and offsets")
func isoTimes() {
    #expect(ISODate.epoch("1970-01-01T00:00:10Z") == 10)
    #expect(ISODate.epoch("1970-01-01T01:00:00+01:00") == 0)
    #expect(ISODate.epoch("1970-01-01T00:00:00.5+00:00") == 0.5)
    #expect(ISODate.epoch("2026-10-11T03:00:00.123456+00:00").map { $0 > 1.79e9 } == true)
    #expect(ISODate.epoch("soon") == nil)
}

@Test("headroom after a window's reset is the whole window")
func settledHeadroom() {
    let data = PyObject([
        ("five_hour", .object(PyObject([("utilization", 90.0), ("resets_at", "1970-01-01T00:00:10Z")]))),
        ("seven_day", .object(PyObject([("utilization", 40.0), ("resets_at", "2099-01-01T00:00:00Z")]))),
    ])
    let (session, weekly) = SwitchEngine.left(data, now: 20)
    #expect(session == 100 && weekly == 60)
}

private final class Ran: @unchecked Sendable { var argv: [[String]] = [] }

@Test("engine: a quiet tick's slots skip until an input changes, then the tick runs and the baseline moves")
func switchEngineSlots() throws {
    var template = Array("/tmp/acc-cored-switch-XXXXXX".utf8CString)
    let home = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: home) }
    let dir = GuardPaths.state(home)
    Files.mkdirs(dir)
    let now = Kernel.wall()
    let write = { (s: PyObject) in _ = Files.writeAtomic(dir + "/state.json", PyJSON.object(s).dumps()) }
    write(state(lastTick: now - 100, quiet: true, at: now + 3600))
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    let ran = Ran()
    engine.runner = { argv in
        ran.argv.append(argv)
        write(state(lastTick: Kernel.wall(), quiet: true, at: Kernel.wall() + 3600))
        return 0
    }
    engine.queue.sync { engine.baseline = engine.stamps() }

    engine.queue.sync { engine.slotFired() }
    #expect(engine.skips == 1 && ran.argv.isEmpty)

    #expect(Files.writeAtomic(dir + "/config.json", #"{"drain": true}"#))
    engine.queue.sync { engine.slotFired() }
    #expect(ran.argv == [["/nonexistent/python", "accswitch", "tick"]])

    // the tick's own writes are the new baseline
    engine.queue.sync { engine.slotFired() }
    #expect(ran.argv.count == 1 && engine.skips == 2)

    write(state(lastTick: Kernel.wall(), quiet: false))
    engine.queue.sync { engine.baseline = engine.stamps() }
    engine.queue.sync { engine.slotFired() }
    #expect(ran.argv.count == 2)
    engine.stop()
}
