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

/// A scratch HOME under /tmp with a state directory and a login Keychain file to stamp.
private func switchHome(_ name: String) throws -> (home: String, dir: String) {
    var template = Array("/tmp/acc-cored-switch-\(name)-XXXXXX".utf8CString)
    let home = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    let dir = GuardPaths.state(home)
    Files.mkdirs(dir)
    Files.mkdirs(home + "/Library/Keychains")
    #expect(Files.writeAtomic(home + "/Library/Keychains/login.keychain-db", "kc"))
    return (home, dir)
}

private func writeState(_ dir: String, _ s: PyObject) {
    _ = Files.writeAtomic(dir + "/state.json", PyJSON.object(s).dumps())
}

@Test("engine: a quiet tick's slots skip until an input changes, then the tick runs and the baseline moves")
func switchEngineSlots() throws {
    let (home, dir) = try switchHome("slots")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    writeState(dir, state(lastTick: now - 100, quiet: true, at: now + 3600))
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    engine.resolver = { nil }
    let ran = Ran()
    engine.runner = { argv, _ in
        ran.argv.append(argv)
        writeState(dir, state(lastTick: Kernel.wall(), quiet: true, at: Kernel.wall() + 3600))
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

    writeState(dir, state(lastTick: Kernel.wall(), quiet: false))
    engine.queue.sync { engine.baseline = engine.stamps() }
    engine.queue.sync { engine.slotFired() }
    #expect(ran.argv.count == 2)
    engine.stop()
}

@Test("engine: the baseline moves only after a tick that ran; changes during it and odd exits arm the next slot")
func switchEngineAfterTick() throws {
    let (home, dir) = try switchHome("after")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    writeState(dir, state(lastTick: now - 100, quiet: true, at: now + 3600))
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    engine.resolver = { nil }
    final class Script: @unchecked Sendable {
        var writesLastTick = true
        var otherWrite = false
        var rc: Int32 = 0
        var deadlines: [Double] = []
    }
    let script = Script()
    engine.runner = { _, env in
        script.deadlines.append(Double(env["ACC_TICK_DEADLINE"] ?? "") ?? 0)
        if script.writesLastTick { writeState(dir, state(lastTick: Kernel.wall() + 1, quiet: true, at: Kernel.wall() + 3600)) }
        if script.otherWrite { _ = Files.writeAtomic(dir + "/pause.json", "{}") }
        return script.rc
    }
    engine.queue.sync { engine.baseline = engine.stamps() }

    // the lock was busy: no last_tick, so the change that ran it stays visible to the next slot
    script.writesLastTick = false
    #expect(Files.writeAtomic(dir + "/config.json", #"{"drain": true}"#))
    engine.queue.sync { engine.slotFired() }
    engine.queue.sync { engine.slotFired() }
    #expect(engine.runs == 2)
    #expect(script.deadlines.allSatisfy { abs($0 - (now + 100)) < 5 })

    // it ran; someone wrote pause.json meanwhile: one more tick, then quiet
    script.writesLastTick = true
    script.otherWrite = true
    engine.queue.sync { engine.slotFired() }
    #expect(engine.runs == 3)
    #expect(engine.queue.sync { engine.pending } == ["changed during the tick: pause.json"])
    script.otherWrite = false
    engine.queue.sync { engine.slotFired() }
    engine.queue.sync { engine.slotFired() }
    #expect(engine.runs == 4 && engine.skips == 1)

    // a crash (or a signal from elsewhere) isn't a quiet result
    script.rc = 139
    #expect(Files.writeAtomic(dir + "/config.json", #"{"drain": false}"#))
    engine.queue.sync { engine.slotFired() }
    #expect(engine.queue.sync { engine.pending } == ["the tick ended with 139"])
    script.rc = 0
    engine.queue.sync { engine.slotFired() }
    #expect(engine.runs == 6)
    engine.stop()
}

@Test("engine: a tick is never signalled past its deadline, leads its own group, and stop() doesn't wait for it")
func switchEngineNeverKills() throws {
    let (home, dir) = try switchHome("nokill")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let tick = dir + "/tick.sh"
    // records every catchable signal; a SIGKILL would leave no "done"
    #expect(Files.writeAtomic(tick, """
        for s in TERM INT HUP USR1 USR2; do trap "echo $s >> '\(dir)/signals'" $s; done
        echo "$ACC_TICK_DEADLINE $$ $(ps -o pgid= -p $$)" > '\(dir)/started'
        sleep 2
        printf '{"last_tick": %s}' "$(date +%s)" > '\(dir)/state.json'
        echo done > '\(dir)/done'
        """))
    var options = SwitchEngine.Options(home: home, python: ["/bin/sh", tick])
    options.tickDeadline = 0.5
    let engine = SwitchEngine(options)
    engine.resolver = { nil }
    let started = Kernel.wall()
    engine.queue.sync { engine.slotFired() }  // no state.json: no tick_due, so it runs
    #expect(engine.queue.sync { engine.running != nil })
    for _ in 0..<100 where Files.read(dir + "/started")?.last != 10 { usleep(20_000) }
    let fields = String(decoding: Files.read(dir + "/started") ?? [], as: UTF8.self).split(whereSeparator: \.isWhitespace)
    #expect(fields.count == 3 && fields[1] == fields[2])  // its pid leads its group
    #expect(fields.first.flatMap { Double($0) }.map { abs($0 - (started + 0.5)) < 2 } == true)
    let before = Kernel.wall()
    engine.stop()
    #expect(Kernel.wall() - before < 0.5)
    for _ in 0..<300 where engine.queue.sync(execute: { engine.running != nil }) { usleep(20_000) }
    #expect(Files.read(dir + "/done") != nil)
    #expect(Files.read(dir + "/signals") == nil)
    #expect(Kernel.wall() - started >= 2)
    let record = Files.json(dir + "/accswitch-slot.json", fallback: .null)
    #expect(record["ran"]?.truthy == true && record["rc"]?.double == 0 && (record["tick_s"]?.double ?? 0) >= 2)
}

@Test("engine: a skipped slot vouches for the status.json it saw; the Keychain stamp covers every keychain file")
func switchEngineRecords() throws {
    let (home, dir) = try switchHome("record")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    writeState(dir, state(lastTick: now - 100, quiet: true, at: now + 3600))
    #expect(Files.writeAtomic(dir + "/status.json", "{\"generated_at\": \(now - 99.5)}"))
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    engine.resolver = { nil }
    engine.runner = { _, _ in 0 }
    engine.queue.sync { engine.baseline = engine.stamps() }
    engine.queue.sync { engine.slotFired() }
    let record = Files.json(dir + "/accswitch-slot.json", fallback: .null)
    #expect(record["ran"]?.truthy == false && record["status_at"]?.double == now - 99.5)
    #expect(record["checked_at"]?.double == record["at"]?.double)
    #expect(engine.status()["skips"]?.double == 1)
    // a status.json older than the last tick (it didn't get to write one): nothing to vouch for
    #expect(Files.writeAtomic(dir + "/status.json", "{\"generated_at\": \(now - 101)}"))
    engine.queue.sync { engine.slotFired() }
    #expect(Files.json(dir + "/accswitch-slot.json", fallback: .null)["status_at"] == .null)

    #expect(Files.writeAtomic(home + "/Library/Keychains/other.keychain-db", "kc"))
    #expect(engine.queue.sync { engine.changedInputs(engine.stamps()) } == ["keychain"])
    try FileManager.default.removeItem(atPath: home + "/Library/Keychains")
    engine.queue.sync { engine.baseline = engine.stamps() }
    engine.queue.sync { engine.slotFired() }  // blind without a keychain file: every slot runs
    #expect(engine.runs == 1)
    #expect(Files.json(dir + "/accswitch-slot.json", fallback: .null)["reasons"]?.array?.contains("no keychain file") == true)
}

@Test("engine: the host is asked again while it fails and when a file its choice depends on changes")
func switchEngineHost() throws {
    let (home, _) = try switchHome("host")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let lock = home + "/SingletonLock"
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    final class Calls: @unchecked Sendable { var n = 0; var answer = false }
    let calls = Calls()
    engine.resolver = {
        calls.n += 1
        return calls.answer ? HostResolver.DataFiles(userData: home, dataFile: "orca-data.json", watch: [lock]) : nil
    }
    engine.queue.sync { engine.refreshHost() }
    engine.queue.sync { engine.refreshHost() }
    #expect(calls.n == 2)
    calls.answer = true
    engine.queue.sync { engine.refreshHost() }
    engine.queue.sync { engine.refreshHost() }
    #expect(calls.n == 3)
    #expect(symlink("Orca-\(getpid())", lock) == 0)  // the host starts: its SingletonLock appears
    engine.queue.sync { engine.refreshHost() }
    engine.queue.sync { engine.refreshHost() }
    #expect(calls.n == 4)
}

@Test("decisions: a last_tick ahead of the clock runs the tick; ISO offsets without a colon")
func switchClockAndOffsets() {
    let now = 10_000.0
    let ahead = SwitchEngine.decide(state: state(lastTick: now + 3600, quiet: true, at: now + 7200), changed: [], now: now, maxGap: 480)
    #expect(ahead.run && ahead.reasons == ["last tick ahead of the clock"])
    #expect(ISODate.epoch("1970-01-01T01:00:00+0100") == 0)
}

@Test("engine: a check is a tick that got the lock and ended with 0; a held lock, a crash or a failed start isn't")
func switchEngineChecks() throws {
    let (home, dir) = try switchHome("checks")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    writeState(dir, state(lastTick: now - 100, quiet: false))  // not quiet: every slot runs
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))
    engine.resolver = { nil }
    final class Tick: @unchecked Sendable { var rc: Int32 = 0; var locked = true; var at = 0.0 }
    let tick = Tick()
    tick.at = now - 100
    engine.runner = { _, _ in
        if tick.locked {  // it got the lock: its first write is last_tick
            tick.at += 1
            writeState(dir, state(lastTick: tick.at, quiet: false))
        }
        return tick.rc
    }
    let sentinel = 1_000.0
    for (rc, locked) in [(Int32(0), false), (1, true), (134, true), (137, true)] {
        tick.rc = rc
        tick.locked = locked
        engine.queue.sync {
            engine.lastHealthy = sentinel
            engine.slotFired()
        }
        let record = Files.json(dir + "/accswitch-slot.json", fallback: .null)
        #expect(record["ran"]?.truthy == true && record["rc"]?.double == Double(rc))
        #expect(record["checked_at"]?.double == sentinel, "rc \(rc), lock taken: \(locked)")
    }
    tick.rc = 0
    tick.locked = true
    engine.queue.sync { engine.slotFired() }
    let record = Files.json(dir + "/accswitch-slot.json", fallback: .null)
    #expect(record["checked_at"]?.double == record["at"]?.double)

    // a start that fails, slot after slot: one log line, not one per slot
    engine.runner = nil
    for _ in 0..<3 { engine.queue.sync { engine.slotFired() } }
    let log = String(decoding: Files.read(dir + "/launchd.log") ?? [], as: UTF8.self)
    #expect(log.components(separatedBy: "the tick didn't start").count == 2)
    engine.stop()
}

@Test("engine: a tick that doesn't start is a tried slot, not a check: the switcher goes silent; ping never waits")
func switchEngineDeadTick() throws {
    let (home, dir) = try switchHome("dead")
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    writeState(dir, state(lastTick: now - 100, quiet: true, at: now + 3600))
    #expect(Files.writeAtomic(dir + "/status.json", "{\"generated_at\": \(now - 99.5)}"))
    let engine = SwitchEngine(.init(home: home, python: ["/nonexistent/python"]))  // no runner: Spawn.start fails
    final class Slow: @unchecked Sendable { var answering = false }
    let slow = Slow()
    engine.resolver = {
        slow.answering = true
        usleep(1_500_000)  // orcahost.py under load
        slow.answering = false
        return nil
    }
    engine.queue.sync { engine.baseline = engine.stamps() }
    engine.queue.sync { engine.slotFired() }  // a skip: a check
    let checked = Files.json(dir + "/accswitch-slot.json", fallback: .null)["checked_at"]?.double
    #expect(checked != nil)

    #expect(Files.writeAtomic(dir + "/config.json", "{}"))
    engine.queue.async { engine.slotFired() }
    for _ in 0..<100 where !slow.answering { usleep(10_000) }
    let start = Kernel.monotonic()
    #expect(engine.status()["runs"] != nil)  // answered while the lookup holds the queue
    #expect(Kernel.monotonic() - start < 0.2)
    engine.queue.sync {}
    let record = Files.json(dir + "/accswitch-slot.json", fallback: .null)
    #expect(record["ran"]?.truthy == true && record["rc"]?.double == 127)
    #expect(record["status_at"] == .null && record["checked_at"]?.double == checked)
    #expect(String(decoding: Files.read(dir + "/launchd.log") ?? [], as: UTF8.self).contains("the tick didn't start"))
    engine.stop()
}
