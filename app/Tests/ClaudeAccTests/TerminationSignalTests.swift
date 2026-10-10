import Darwin
import Foundation
import Synchronization
import Testing
@testable import ClaudeAcc

@Test("SIGTERM reaches the quit handler instead of ending the process")
func sigtermQuitsThroughTheHandler() async throws {
    let quits = Mutex(0)
    let handler = TerminationSignal(queue: DispatchQueue(label: "TerminationSignalTests")) {
        quits.withLock { $0 += 1 }
    }
    defer { handler.cancel() }
    kill(getpid(), SIGTERM)
    for _ in 0..<200 where quits.withLock({ $0 }) == 0 { try await Task.sleep(for: .milliseconds(10)) }
    #expect(quits.withLock { $0 } == 1)
}
