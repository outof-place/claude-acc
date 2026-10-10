import Testing
@testable import AccCore

// The guard's arithmetic as CPython does it.

@Test("sum() of floats is compensated since Python 3.12, and an empty sum is the int 0")
func compensatedSum() {
    #expect(pySum([Double]()) == .int(0))
    #expect(pySum([0.1, 0.2, 0.3]) == .double(0.6))  // plain left-to-right gives 0.6000000000000001
    #expect(pySum([1e100, 1.0, -1e100]) == .double(1.0))
    #expect(pySum([PyNum.int(0), .double(0.1), .int(2), .double(0.2)]) == pySum([0.1, 2, 0.2]))
}
