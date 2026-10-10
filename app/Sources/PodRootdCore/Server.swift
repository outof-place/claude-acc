import Foundation
import PodRootdProtocol
import XPC

/// The XPC side: a listener whose sessions run on the main queue, each message classified by the
/// peer policy and handed to the engine; a session's end drops its leases.
public final class Server {
    public let engine: Engine
    private let peers: PeerPolicy
    private var listener: XPCListener?
    private var nextSession: UInt64 = 0
    public private(set) var sessions = 0
    /// After each message and each session's end: the daemon reschedules its tick and its idle exit.
    public var onActivity: (() -> Void)?

    public init(engine: Engine, peers: PeerPolicy) {
        self.engine = engine
        self.peers = peers
    }

    /// The Mach service from the helper's plist. The system checks `peers.listener` on every
    /// message before any handler runs.
    public func listen(service: String) throws {
        let peers = peers
        listener = try XPCListener(service: service, targetQueue: .main, requirement: peers.listener) { request in
            Self.accept(request, server: self, peers: peers)
        }
    }

    /// An anonymous listener, for tests: no requirement from the system, only the per-message check.
    public func listenAnonymously() -> XPCEndpoint {
        let peers = peers
        let listener = XPCListener(targetQueue: .main) { request in
            Self.accept(request, server: self, peers: peers)
        }
        self.listener = listener
        return listener.endpoint
    }

    public func cancel() {
        listener?.cancel()
        listener = nil
    }

    /// On the listener's queue (main): a session number, then a handler whose session also runs on
    /// the main queue.
    nonisolated private static func accept(
        _ request: XPCListener.IncomingSessionRequest, server: Server, peers: PeerPolicy
    ) -> XPCListener.IncomingSessionRequest.Decision {
        let id = MainActor.assumeIsolated { server.opened() }
        let handler = PeerHandler(server: server, session: id, peers: peers)
        return request.accept { (session: XPCSession) in
            session.setTargetQueue(.main)
            return handler
        }
    }

    private func opened() -> SessionID {
        nextSession += 1
        sessions += 1
        return SessionID(nextSession)
    }

    fileprivate func handle(_ request: Request?, from caller: Caller?, session: SessionID) -> Reply {
        defer { onActivity?() }
        return engine.handle(request, from: caller, session: session)
    }

    fileprivate func ended(_ session: SessionID) {
        sessions -= 1
        engine.sessionEnded(session)
        onActivity?()
    }
}

/// One accepted session: it names the caller of each message and passes it on to the main actor.
nonisolated private struct PeerHandler: XPCPeerHandler {
    let server: Server
    let session: SessionID
    let peers: PeerPolicy

    func handleIncomingRequest(_ message: XPCReceivedMessage) -> (any Encodable)? {
        let caller = peers.classify { message.senderSatisfies($0) }
        let request = try? message.decode(as: Request.self)
        return MainActor.assumeIsolated { server.handle(request, from: caller, session: session) }
    }

    func handleCancellation(error: XPCRichError) {
        MainActor.assumeIsolated { server.ended(session) }
    }
}
