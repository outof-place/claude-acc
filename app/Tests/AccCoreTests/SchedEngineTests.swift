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
