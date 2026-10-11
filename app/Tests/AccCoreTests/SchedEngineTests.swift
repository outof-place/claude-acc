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
    // the cleanup comes first: a spawn that fails mustn't leave the earlier ones running
    final class Kids: @unchecked Sendable { var pids: [pid_t] = [] }
    let kids = Kids()
    defer {
        for p in kids.pids {
            kill(p, SIGKILL)
            var status: Int32 = 0
            waitpid(p, &status, 0)
        }
    }
    let spawn = { (argv: [String]) -> pid_t? in
        let pid = Spawn.start(argv)
        if let pid { kids.pids.append(pid) }
        return pid
    }
    let wrapper = try #require(spawn(["/bin/sh", dir + "/sched.py"]))
    let other = try #require(spawn(["/bin/sleep", "30"]))
    let idle = try #require(spawn(["/bin/sh", dir + "/sched.py"]))
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

@Test("reap signals only a dead wrapper's own group from this boot: no stale, reused, own or non-group")
func reapSignals() throws {
    let boot = 1_000_000.0, fresh = boot + 60, own = 777
    // the p_uniqueid an entry names for a pid; the kernel gives the same one while it's that process,
    // another once the pid is reused
    let stamp = { (pid: Int) in 30_000_000 + pid * 10 }
    var starts: [(String, PyJSON)] = []
    enum Live { case mine, reused, none }
    let run = { (id: String, pid: Int, pgid: PyJSON, at: Double?, wrapper: Live, group: Live, stamped: Bool) -> PyJSON in
        var o = PyObject([("id", .string(id)), ("where", "local"), ("pid", .int(pid)), ("child_pgid", pgid)])
        o["enqueued_at"] = .double((at ?? fresh) - 10)
        if let at { o["started_at"] = .double(at) }
        o["pid_uid"] = .int(stamp(pid))
        switch wrapper {
        case .mine: starts.append((String(pid), .int(stamp(pid))))
        case .reused: starts.append((String(pid), .int(stamp(pid) + 1)))
        case .none: break
        }
        if let g = pgid.int, g > 0 {
            if stamped { o["child_uid"] = .int(stamp(g)) }
            switch group {
            case .mine: starts.append((String(g), .int(stamp(g))))
            case .reused: starts.append((String(g), .int(stamp(g) + 1)))
            case .none: break
            }
        }
        return .object(o)
    }
    let queued = { (id: String, pid: Int, at: Double, live: Live) -> PyJSON in
        if live == .mine { starts.append((String(pid), .int(stamp(pid)))) }
        if live == .reused { starts.append((String(pid), .int(stamp(pid) + 1))) }
        return .object(PyObject([("id", .string(id)), ("pid", .int(pid)), ("enqueued_at", .double(at)), ("pid_uid", .int(stamp(pid)))]))
    }
    let before = PyObject([
        ("running", .array([
            run("orphan", 101, .int(201), fresh, .none, .mine, true),  // a dead wrapper, its own group: SIGCONT + SIGTERM
            run("stale", 102, .int(202), boot - 60, .none, .mine, true),  // from before the boot
            run("stale-alive", 103, .int(203), boot - 60, .mine, .mine, true),
            run("own", 104, .int(own), fresh, .none, .mine, true),  // the reaper's own process group
            run("negative", 105, .int(-205), fresh, .none, .none, true), run("zero", 106, .int(0), fresh, .none, .none, true),
            run("one", 107, .int(1), fresh, .none, .mine, true), run("flag", 108, .bool(true), fresh, .none, .none, true),
            run("wide", 109, .int(1 << 40), fresh, .none, .none, true), run("text", 110, "211", fresh, .none, .none, true),
            run("live", 111, .int(212), fresh, .mine, .mine, true),
            // pids wrap every ~25 min: the wrapper's pid lives but is another process now, and so is its group
            run("reused-wrapper", 112, .int(213), fresh, .reused, .reused, true),
            run("reused-group", 113, .int(214), fresh, .none, .reused, true),  // a dead wrapper, someone else's group
            run("no-child-stamp", 114, .int(215), fresh, .none, .mine, false),  // nothing to check the group by
            run("no-started-at", 115, .int(216), nil, .none, .mine, true),
        ])),
        ("queue", .array([
            queued("q-stale", 301, boot - 1, .mine), queued("q-live", 302, fresh, .mine), queued("q-dead", 303, fresh, .none),
            queued("q-reused", 304, fresh, .reused),
        ])),
    ])
    // every pid and group above is alive (kill(-205, 0) probes group 205, 1 is launchd), so only the
    // checks keep them from a signal
    let alive: [PyJSON] = [201, 202, 103, 203, own, 205, -205, 1, 211, 111, 212, 1 << 40, 112, 213, 214, 215, 216, 301, 302, 304].map { .int($0) }
    let probes = RecordedSchedProbes(PyObject([("alive", .array(alive)), ("boottime", .double(boot)), ("pgrp", .int(own)),
                                               ("uids", .object(PyObject(starts)))]))
    var state = before
    Sched.reap(&state, probes)
    let ids = { (key: String) in (state[key]?.array ?? []).map { $0["id"]?.string ?? "" } }
    #expect(probes.kills == [.array([.int(201), .int(Int(SIGCONT))]), .array([.int(201), .int(Int(SIGTERM))])])
    #expect(ids("running") == ["live"])
    #expect(ids("queue") == ["q-live"])

    // an unknown boot time (0) makes nothing stale: the same old entry is then signalled, so the
    // boot rule is what spared group 202 above
    let blind = RecordedSchedProbes(PyObject([("alive", .array(alive)), ("pgrp", .int(own)), ("uids", .object(PyObject(starts)))]))
    state = before
    Sched.reap(&state, blind)
    let groups = blind.kills.compactMap { $0.array?.first?.int }
    #expect(groups == [201, 201, 202, 202])
    #expect(ids("running") == ["stale-alive", "live"])

    // an entry without pid_uid (written before it existed), or with one no Int64 holds: alive is
    // enough, as before; a uid that isn't one gets no group signal either
    var old = PyObject([("running", .array([.object(PyObject([("id", "old"), ("where", "local"), ("pid", .int(111)), ("started_at", .double(fresh))]))])),
                        ("queue", .array([]))])
    Sched.reap(&old, probes)
    #expect((old["running"]?.array ?? []).count == 1)
    let oddUID = { (id: String, uid: PyJSON) -> PyJSON in
        .object(PyObject([("id", .string(id)), ("where", "local"), ("pid", .int(111)), ("pid_uid", uid), ("started_at", .double(fresh))]))
    }
    var odd = PyObject([("running", .array([oddUID("big", .raw("1180591620717411303424")), oddUID("negative", .int(-3))])),
                        ("queue", .array([]))])
    Sched.reap(&odd, probes)
    #expect((odd["running"]?.array ?? []).count == 2)
    #expect(Sched.groupOf(.object(PyObject([("child_pgid", .int(212)), ("child_uid", .raw("1180591620717411303424"))])), probes) == nil)
    #expect(Sched.groupOf(.object(PyObject([("child_pgid", .int(212)), ("child_uid", .int(-3))])), probes) == nil)
}

@Test("safety pauses the youngest heavy job whose group is still its own: never a reused or unstamped group")
func safetySignals() {
    let now = 1_000_000.0
    let job = { (id: String, pgid: Int, started: Double, stamp: Int?) -> PyJSON in
        var o = PyObject([("id", .string(id)), ("where", "local"), ("pid", .int(1)), ("child_pgid", .int(pgid)), ("started_at", .double(started))])
        if let stamp { o["child_uid"] = .int(stamp) }
        return .object(o)
    }
    var state = PyObject([
        ("running", .array([
            job("a", 401, now - 30, 7001), job("b", 402, now - 20, 7002),
            job("c", 403, now - 10, 7003),  // the youngest, but its group is another process now
            job("d", 404, now - 5, nil),  // no child_uid: no way to check
        ])),
        ("memory", .object(PyObject([("swap_growth_2m_gb", 99), ("level_pct", 10), ("pressure", "critical")]))),
        ("today", .object(PyObject([("pauses", 0)]))),
    ])
    let starts = PyObject([("401", .int(7001)), ("402", .int(7002)), ("403", .int(9999)), ("404", .int(7004))])
    let probes = RecordedSchedProbes(PyObject([("now", .double(now)), ("uids", .object(starts))]))
    Sched.safety(&state, SchedConfig(raw: SchedConfig.defaults), probes)
    #expect(probes.kills == [.array([.int(402), .int(Int(SIGSTOP))])])
    #expect(state["running"]?.array?[1]["paused"]?.truthy == true)
}

@Test("the live boot time is now minus the time since boot; a process's p_uniqueid is its own, also for root's")
func liveBootAndStart() {
    let boot = SchedNative.bootTime()
    let wall = Kernel.wall()
    let up = Double(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1e9
    #expect(boot > 0 && boot < wall)
    #expect(abs((wall - boot) - up) < 5)
    let me = SchedNative.uniqueID(getpid())
    #expect(me != nil)
    #expect(SchedNative.uniqueID(getpid()) == me)
    #expect(SchedNative.uniqueID(getppid()) != me)
    #expect(SchedNative.uniqueID(1) != nil)  // launchd, root's: proc_bsdinfo would give EPERM here
    #expect(SchedNative.uniqueID(pid_t(Int32.max)) == nil)
}

@Test("no watch on an entry from before the boot or on a group that isn't the job's")
func watchesOnlyOwnGroups() throws {
    var template = Array("/tmp/acc-cored-watch-XXXXXX".utf8CString)
    let dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: dir) }
    Files.mkdirs(dir + "/sched")
    let engine = SchedEngine(stateDir: dir)
    let now = Kernel.wall()
    let job = { (id: String, started: Double, stamp: Int) in
        PyObject([("id", .string(id)), ("monitor", "cored"), ("pid", .int(Int(getpid()))), ("child_pgid", .int(Int(getpid()))),
                  ("started_at", .double(started)), ("child_started_at", .double(started)), ("child_uid", .int(stamp))])
    }
    engine.queue.sync {
        engine.updateWatches([job("old", 100, 42), job("reused", now, 41), job("mine", now, 42)], now: now,
                             uid: { _ in 42 }, boot: 1000)
    }
    #expect(engine.queue.sync { Set(engine.watches.keys) } == ["mine"])
    engine.queue.sync { engine.standBack() }
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
