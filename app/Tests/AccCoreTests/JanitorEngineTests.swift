import Darwin
import Testing
@testable import AccCore

// The janitor's sweep waits, once due, for an idle Mac.

private func reading(now: Double = 100_000, last: Double = 100_000 - 4 * 3600, attempt: Double = 0, minGap: Double = 2 * 3600,
                     running: Bool = false, idle: Double? = 600, builds: Int = 0, uptime: Double = 3600,
                     battery: (Bool, Int) = (false, 100)) -> JanitorEngine.Reading {
    JanitorEngine.Reading(now: now, lastSweep: last, lastAttempt: attempt, minGap: minGap, running: running, idle: idle,
                          builds: builds, uptime: uptime, onBattery: battery.0, percent: battery.1, minBattery: 30)
}

private func decide(_ r: JanitorEngine.Reading) -> JanitorEngine.Decision {
    JanitorEngine.decide(r, every: 3 * 3600, idleSeconds: 300, maxDefer: 6 * 3600)
}

@Test("a due sweep runs on an idle Mac; in use it waits for the idle time it lacks")
func janitorIdle() {
    #expect(decide(reading()).run)
    let busy = decide(reading(idle: 30))
    #expect(!busy.run && busy.next == 100_000 + 270 && busy.why == "in use")
    #expect(decide(reading(last: 100_000 - 3600)) == .init(run: false, next: 100_000 + 2 * 3600, why: "not due"))
}

@Test("builds, a running sweep, a fresh boot and a low battery hold it; overdue it runs even in use")
func janitorHolds() {
    #expect(decide(reading(builds: 2)).why == "builds running")
    #expect(decide(reading(running: true)).why == "a sweep is running")
    #expect(decide(reading(uptime: 100)).next == 100_000 + 200)
    #expect(decide(reading(battery: (true, 20))).why == "low battery")
    #expect(decide(reading(battery: (true, 80))).run)
    #expect(decide(reading(last: 100_000 - 10 * 3600, idle: 1, builds: 3)).run)
    #expect(!decide(reading(last: 100_000 - 10 * 3600, battery: (true, 10))).run)
    let due: Double = 100_000 - 3600  // four hours since the last sweep, every three
    #expect(decide(reading(idle: nil)).next == due + 6 * 3600)
}

@Test("overdue counts from the due time, and idle means no input for the whole idle time")
func janitorEdges() {
    let due: Double = 100_000 - 3600
    #expect(decide(reading(now: due + 6 * 3600 - 1, idle: 1)).why == "in use")
    #expect(decide(reading(now: due + 6 * 3600, idle: 1)) == .init(run: true, next: 0, why: "overdue"))
    #expect(decide(reading(idle: 300)).run)
    #expect(decide(reading(idle: 299.5)) == .init(run: false, next: 100_000 + 0.5, why: "in use"))
}

@Test("an attempt counts like launchd's start, and Python's min_hours_between_sweeps holds it too")
func janitorAttempts() {
    // a sweep started ten minutes ago that didn't record itself: the next try is `every` after it
    #expect(decide(reading(attempt: 100_000 - 600)) == .init(run: false, next: 100_000 - 600 + 3 * 3600, why: "not due"))
    #expect(decide(reading(attempt: 100_000 - 3 * 3600)).run)
    // min_hours_between_sweeps 12: four hours after the last sweep, Python would skip it
    #expect(decide(reading(minGap: 12 * 3600)) == .init(run: false, next: 100_000 - 4 * 3600 + 12 * 3600, why: "not due"))
}

private final class Ran: @unchecked Sendable {
    var argv: [[String]] = []
    var current = reading(now: Kernel.wall(), last: Kernel.wall() - 4 * 3600)
}

@Test("engine: a due, idle reading starts the sweep in the background band, then looks again")
func janitorEngineRuns() async throws {
    let engine = JanitorEngine(.init(home: "/nonexistent", python: ["/py", "/acc.py"]))
    let ran = Ran()
    engine.reader = { ran.current }
    engine.runner = { argv in
        ran.argv.append(argv)
        ran.current.lastSweep = Kernel.wall()  // the sweep records itself
        ran.current.now = Kernel.wall()
        return 0
    }
    engine.start()
    // the sweep starts on a background-QoS thread, which a loaded Mac may hold back for seconds
    for _ in 0..<3000 where engine.queue.sync(execute: { engine.sweeps }) == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
    #expect(ran.argv == [["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10", "/py", "/acc.py", "janitor", "sweep"]])
    #expect(engine.queue.sync { engine.sweeps } == 1)
    engine.stop()
}

@Test("engine: a sweep that doesn't record itself runs once, not again at once; after stop() nothing starts")
func janitorEngineBacksOff() async throws {
    let engine = JanitorEngine(.init(home: "/nonexistent", python: ["/py", "/acc.py"]))
    let ran = Ran()
    engine.reader = { ran.current }
    engine.runner = { argv in
        ran.argv.append(argv)  // no Python, an exception, its own gate: last_sweep stays
        return 1
    }
    engine.start()
    for _ in 0..<3000 where engine.queue.sync(execute: { engine.sweeps }) == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
    try await Task.sleep(nanoseconds: 500_000_000)
    #expect(engine.queue.sync { engine.sweeps } == 1)
    #expect(ran.argv.count == 1)
    engine.stop()
    engine.queue.sync { engine.lastAttempt = 0 }  // due again, but stopped
    engine.queue.sync { engine.check() }
    #expect(!engine.queue.sync { engine.sweeping })
}
