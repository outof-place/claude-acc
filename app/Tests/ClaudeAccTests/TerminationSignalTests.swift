import Darwin
import Foundation
import Synchronization
import Testing
@testable import ClaudeAcc

private func until(_ condition: () -> Bool) async throws {
    for _ in 0..<200 where !condition() { try await Task.sleep(for: .milliseconds(10)) }
}

// One suite, run serially: each test raises SIGTERM in this test process.
@Suite(.serialized) struct TerminationSignalTests {
    @Test("SIGTERM reaches the quit handler instead of ending the process")
    func sigtermQuitsThroughTheHandler() async throws {
        let quits = Mutex(0), exits = Mutex(0)
        let handler = TerminationSignal(
            queue: DispatchQueue(label: "TerminationSignalTests"), grace: .seconds(60),
            exit: { exits.withLock { $0 += 1 } }, quit: { quits.withLock { $0 += 1 } })
        defer { handler.cancel() }
        kill(getpid(), SIGTERM)
        try await until { quits.withLock { $0 } == 1 }
        #expect(quits.withLock { $0 } == 1)
        #expect(exits.withLock { $0 } == 0)
    }

    @Test("a quit that hangs, or a second SIGTERM, ends the process anyway")
    func stuckQuitOrSecondSigtermExits() async throws {
        let exits = Mutex(0)
        let stuck = TerminationSignal(
            queue: DispatchQueue(label: "TerminationSignalTests.grace"), grace: .milliseconds(100),
            exit: { exits.withLock { $0 += 1 } }, quit: {})
        kill(getpid(), SIGTERM)
        try await until { exits.withLock { $0 } == 1 }
        #expect(exits.withLock { $0 } == 1)
        stuck.cancel()

        exits.withLock { $0 = 0 }
        let twice = TerminationSignal(
            queue: DispatchQueue(label: "TerminationSignalTests.twice"), grace: .seconds(60),
            exit: { exits.withLock { $0 += 1 } }, quit: {})
        defer { twice.cancel() }
        kill(getpid(), SIGTERM)
        try await Task.sleep(for: .milliseconds(100))
        kill(getpid(), SIGTERM)
        try await until { exits.withLock { $0 } == 1 }
        #expect(exits.withLock { $0 } == 1)
    }
}
