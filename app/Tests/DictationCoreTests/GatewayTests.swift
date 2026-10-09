import Foundation
import Testing
@testable import DictationCore

/// A Gateway on this Mac: answers each request after the delay its script gives that request.
final class ScriptedGateway: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var delays: [Double] = []
    nonisolated(unsafe) static var statuses: [Int] = []
    nonisolated(unsafe) static var count = 0
    static let lock = NSLock()

    static func reset(delays: [Double], statuses: [Int] = []) {
        lock.withLock {
            self.delays = delays
            self.statuses = statuses
            count = 0
        }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let (n, delay, status) = Self.lock.withLock {
            Self.count += 1
            let i = Self.count - 1
            return (Self.count, i < Self.delays.count ? Self.delays[i] : 0, i < Self.statuses.count ? Self.statuses[i] : 200)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [self] in
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(#"{"text":"answer \#(n)","error":{"message":"refused"}}"#.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {}

    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ScriptedGateway.self]
        return URLSession(configuration: config)
    }
}

// one at a time: the script is shared
@Suite(.serialized)
struct GatewayTests {
    @Test("a refused key fails at once, without a retry")
    func refusal() async throws {
        ScriptedGateway.reset(delays: [0.05, 0], statuses: [401, 200])
        let gateway = Gateway(key: "k", session: ScriptedGateway.session())
        let started = ContinuousClock.now
        do {
            _ = try await gateway.transcribe(
                Data(count: 100), mediaType: "audio/wav", seconds: 1, model: "microsoft/mai-transcribe-2",
                language: "pl", terms: [], zdr: true, onAttempt: { _ in })
            Issue.record("a 401 went through")
        } catch let error as DictateError {
            #expect(error.code == "auth")
            #expect(error.message.contains("refused"))
        }
        #expect(started.duration(to: .now) < .seconds(1))
        #expect(ScriptedGateway.lock.withLock { ScriptedGateway.count } == 1)
    }

    @Test("a server error is tried again, and the second try answers")
    func retry() async throws {
        ScriptedGateway.reset(delays: [0.02, 0.02], statuses: [503, 200])
        let gateway = Gateway(key: "k", session: ScriptedGateway.session())
        let attempts = AttemptLog()
        let t = try await gateway.transcribe(
            Data(count: 100), mediaType: "audio/wav", seconds: 1, model: "microsoft/mai-transcribe-2", language: "pl",
            terms: [], zdr: true, onAttempt: { attempts.add($0) })
        #expect(t.text == "answer 2")
        #expect(t.attempt == 2)
        #expect(attempts.values == [1, 2])
    }
}

final class AttemptLog: @unchecked Sendable {
    private let lock = NSLock()
    private var log: [Int] = []
    func add(_ n: Int) { lock.withLock { log.append(n) } }
    var values: [Int] { lock.withLock { log } }
}
