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
