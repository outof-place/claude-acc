import Darwin
import Testing
@testable import AccCore

// The janitor's sweep waits, once due, for an idle Mac.

private func reading(now: Double = 100_000, last: Double = 100_000 - 4 * 3600, running: Bool = false, idle: Double? = 600,
                     builds: Int = 0, uptime: Double = 3600, battery: (Bool, Int) = (false, 100)) -> JanitorEngine.Reading {
    JanitorEngine.Reading(now: now, lastSweep: last, running: running, idle: idle, builds: builds, uptime: uptime,
                          onBattery: battery.0, percent: battery.1, minBattery: 30)
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
    for _ in 0..<50 where engine.queue.sync(execute: { engine.sweeps }) == 0 { try await Task.sleep(nanoseconds: 20_000_000) }
    #expect(ran.argv == [["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10", "/py", "/acc.py", "janitor", "sweep"]])
    #expect(engine.queue.sync { engine.sweeps } == 1)
    engine.stop()
}
