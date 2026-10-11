import Darwin
import Foundation
import Testing
@testable import AccCore

// The scheduler's pass: who gets a signal.

@Test("a child_pgid that names no process group above 1 is no group")
func groupIDs() {
    for bad: PyJSON in [.int(-4242), .int(0), .int(1), .bool(true), .double(3.7), .string("4242"), .int(Int(Int32.max) + 1), .null] {
        #expect(Sched.groupID(bad) == nil)
    }
    #expect(Sched.groupID(nil) == nil)
    #expect(Sched.groupID(.int(4242)) == 4242)
}

@Test("SIGUSR1 goes only to a due queued wrapper: never to -1, 0, 1, a non-wrapper or a pid of another type")
func wakeTargets() throws {
    var template = Array("/tmp/acc-cored-wake-XXXXXX".utf8CString)
    let dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: dir) }
    // a "wrapper": argv[1] ends with sched.py, as is_wrapper reads it; it records the SIGUSR1 it gets
    #expect(Files.writeAtomic(dir + "/sched.py", "trap 'echo usr1 >> \(dir)/got' USR1\nwhile :; do sleep 0.05; done\n"))
    // Spawn.start resets every signal to its default: an ignored SIGUSR1 can't be trapped by sh
    let spawn = { (argv: [String]) in Spawn.start(argv) }
    let wrapper = try #require(spawn(["/bin/sh", dir + "/sched.py"]))
    let other = try #require(spawn(["/bin/sleep", "30"]))
    let idle = try #require(spawn(["/bin/sh", dir + "/sched.py"]))
    defer {
        for p in [wrapper, other, idle] {
            kill(p, SIGKILL)
            var status: Int32 = 0
            waitpid(p, &status, 0)
        }
    }
    usleep(200_000)  // the shells read their scripts and set the trap
    let entry = { (id: String, pid: PyJSON) in PyObject([("id", .string(id)), ("pid", pid), ("wake", "usr1")]) }
    let queued = [
        entry("minus-one", .int(-1)), entry("zero", .int(0)), entry("one", .int(1)), entry("bool", .bool(true)),
        entry("float", .double(Double(wrapper))), entry("string", .string(String(wrapper))),
        entry("wide", .int(Int(UInt32.max) + Int(wrapper))), entry("raw", .raw("100000000000000000000")),
        entry("other", .int(Int(other))), entry("wrapper", .int(Int(wrapper))), entry("not-due", .int(Int(idle))),
        PyObject([("id", "polling"), ("pid", .int(Int(wrapper)))]),  // no `wake: usr1`: it polls on its own
    ]
    let admitted = Set(queued.filter { $0["id"] != .string("not-due") }.map { $0["id"] ?? .null })
    final class Sent: @unchecked Sendable { var pids: [pid_t] = [] }
    let sent = Sent()
    let n = SchedEngine.wake(queued, admitted: admitted) { pid in
        sent.pids.append(pid)
        // a real signal only to the test's own wrapper: a wrong target must not reach anyone
        return pid == wrapper && kill(pid, SIGUSR1) == 0
    }
    #expect(sent.pids == [wrapper])
    #expect(n == 1)
    for _ in 0..<50 where Files.read(dir + "/got") == nil { usleep(20_000) }
    #expect(Files.read(dir + "/got") != nil)

    // cancelled counts as due; not admitted and not cancelled doesn't
    sent.pids = []
    let cancelled = [PyObject([("id", "c"), ("pid", .int(Int(idle))), ("wake", "usr1"), ("cancelled", true)])]
    #expect(SchedEngine.wake(cancelled, admitted: []) { sent.pids.append($0); return false } == 0)
    #expect(sent.pids == [idle])
}

@Test("with every pid reading as a wrapper, only a pid above 1 and within pid_t is signalled")
func wakeGuardsAlone() {
    let entry = { (id: String, pid: PyJSON) in PyObject([("id", .string(id)), ("pid", pid), ("wake", "usr1")]) }
    let queued = [
        entry("minus-one", .int(-1)), entry("zero", .int(0)), entry("one", .int(1)), entry("bool", .bool(true)),
        entry("float", .double(4242)), entry("string", "4242"), entry("wide", .int(Int(Int32.max) + 1)),
        entry("raw", .raw("100000000000000000000")), entry("ok", .int(4242)), entry("max", .int(Int(Int32.max))),
    ]
    final class Sent: @unchecked Sendable { var pids: [pid_t] = [] }
    let sent = Sent()
    // no signal leaves the test: the guard alone has to stop the bad pids, not is_wrapper
    let n = SchedEngine.wake(queued, admitted: Set(queued.map { $0["id"] ?? .null }), isWrapper: { _ in true }) { pid in
        sent.pids.append(pid)
        return true
    }
    #expect(sent.pids == [4242, Int32.max])
    #expect(n == 2)
}

@Test("reap signals only a dead wrapper's live group from this boot: no stale entry, own group or non-group")
func reapSignals() throws {
    let boot = 1_000_000.0, fresh = boot + 60, own = 777
    let run = { (id: String, pid: Int, pgid: PyJSON, at: Double) -> PyJSON in
        .object(PyObject([("id", .string(id)), ("where", "local"), ("pid", .int(pid)), ("child_pgid", pgid),
                          ("started_at", .double(at))]))
    }
    let queued = { (id: String, pid: Int, at: Double) -> PyJSON in
        .object(PyObject([("id", .string(id)), ("pid", .int(pid)), ("enqueued_at", .double(at))]))
    }
    let before = PyObject([
        ("running", .array([
            run("orphan", 101, .int(201), fresh),  // a dead wrapper, its group alive: SIGCONT + SIGTERM
            run("stale", 102, .int(202), boot - 60),  // from before the boot: group 202 is someone else's now
            run("stale-alive", 103, .int(203), boot - 60),  // pid 103 alive, but another process now
            run("own", 104, .int(own), fresh),  // the reaper's own process group
            run("negative", 105, .int(-205), fresh), run("zero", 106, .int(0), fresh), run("one", 107, .int(1), fresh),
            run("flag", 108, .bool(true), fresh), run("wide", 109, .int(1 << 40), fresh), run("text", 110, "211", fresh),
            run("live", 111, .int(212), fresh),
        ])),
        ("queue", .array([queued("q-stale", 301, boot - 1), queued("q-live", 302, fresh), queued("q-dead", 303, fresh)])),
    ])
    // every group above is alive (kill(-205, 0) probes group 205, 1 is launchd), so only the checks
    // keep them from a signal
    let alive: [PyJSON] = [201, 202, 103, 203, own, 205, -205, 1, 211, 111, 212, 1 << 40, 301, 302].map { .int($0) }
    let probes = RecordedSchedProbes(PyObject([("alive", .array(alive)), ("boottime", .double(boot)), ("pgrp", .int(own))]))
    var state = before
    Sched.reap(&state, probes)
    let ids = { (key: String) in (state[key]?.array ?? []).map { $0["id"]?.string ?? "" } }
    #expect(probes.kills == [.array([.int(201), .int(Int(SIGCONT))]), .array([.int(201), .int(Int(SIGTERM))])])
    #expect(ids("running") == ["live"])
    #expect(ids("queue") == ["q-live"])

    // an unknown boot time (0) makes nothing stale: the same old entry is then signalled, so the
    // boot rule is what spared group 202 above
    let blind = RecordedSchedProbes(PyObject([("alive", .array(alive)), ("pgrp", .int(own))]))
    state = before
    Sched.reap(&state, blind)
    let groups = blind.kills.compactMap { $0.array?.first?.int }
    #expect(groups == [201, 201, 202, 202])
    #expect(ids("running") == ["stale-alive", "live"])
}

@Test("a pass that stands back (a config read otherwise) drops its watches and their samples")
func standBackDropsWatches() throws {
    var template = Array("/tmp/acc-cored-standback-XXXXXX".utf8CString)
    let dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: dir) }
    Files.mkdirs(dir + "/sched")
    #expect(Files.writeAtomic(dir + "/sched/config.json", #"{"stall_s": "600"}"#))
    let engine = SchedEngine(stateDir: dir)
    engine.queue.sync {
        engine.watches["job"] = SchedEngine.JobWatch(
            id: "job", pgid: getpid(), wrapper: getpid(), started: Kernel.wall(), native: false, simsSince: nil)
        engine.scheduleSamples()
    }
    #expect(engine.queue.sync { engine.sampleTimer != nil })
    engine.queue.sync { engine.pass() }
    #expect(engine.queue.sync { engine.watches.isEmpty && engine.sampleTimer == nil })
}
