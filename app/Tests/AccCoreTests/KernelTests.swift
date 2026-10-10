import Darwin
import Testing
@testable import AccCore

// The readings acc-cored measures with, against what the kernel says through other doors.

@Test("sysctl integers of any width, nil for a name this macOS doesn't have")
func sysctlWidths() {
    #expect(Kernel.int("hw.memsize").map { $0 > 1 << 30 } == true)  // 8 bytes
    #expect(Kernel.int("kern.memorystatus_level").map { $0 <= 100 } == true)  // 4 bytes
    #expect(Kernel.int("acc.cored.no.such.name") == nil)
}

@Test("our own rusage: a start time, a footprint, CPU that only grows")
func ownUsage() throws {
    let a = try #require(Usage.of(getpid()))
    var x = 0.0
    for i in 0..<2_000_000 { x += Double(i) }
    #expect(x > 0)
    let b = try #require(Usage.of(getpid()))
    #expect(a.start == b.start && a.start > 0)
    #expect(b.footprint > 0)
    #expect(b.cpu >= a.cpu)
    #expect(Usage.of(-1) == nil)
}

@Test("a coalition that doesn't exist reads as nil")
func noCoalition() {
    #expect(CoalitionUsage.read(UInt64.max - 1) == nil)
}

@Test("children: all of them, past the first buffer (proc_listchildpids answers a count)")
func manyChildren() {
    var pids: [pid_t] = []
    let argv: [UnsafeMutablePointer<CChar>?] = [strdup("/bin/sleep"), strdup("5"), nil]
    defer { argv.forEach { free($0) } }
    for _ in 0..<300 {
        var pid: pid_t = 0
        if posix_spawn(&pid, "/bin/sleep", nil, nil, argv, environ) == 0 { pids.append(pid) }
    }
    let children = Set(Proc.children(of: getpid()))
    #expect(Set(pids).isSubset(of: children))
    for pid in pids {
        kill(pid, SIGKILL)
        var status: Int32 = 0
        waitpid(pid, &status, 0)
    }
}

@Test("Spawn: a child that outlives its deadline is killed with its group")
func spawnDeadline() {
    let start = Kernel.wall()
    #expect(Spawn.run(["/bin/sh", "-c", "sleep 30 & sleep 30"], timeout: 0.3) == 124)
    #expect(Spawn.output(["/bin/sh", "-c", "exec >&-; sleep 30"], timeout: 0.3) == nil)
    #expect(Spawn.output(["/bin/echo", "hi"], timeout: 5) == Array("hi\n".utf8))
    #expect(Kernel.wall() - start < 5)
}
