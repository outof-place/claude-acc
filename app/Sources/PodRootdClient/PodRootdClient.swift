import LightweightCodeRequirements
import XPC

@_exported import PodRootdProtocol

/// A connection to pod-rootd: one XPC session. Leases (a lid hold, a session-scoped upload limit)
/// last as long as the session, so keep the client for as long as they should hold and `close()` it
/// (or let the process end) to let go.
public final class PodRootdClient: Sendable {
    private let session: XPCSession

    /// The helper of the Pod build with this bundle id: looked up in the system bootstrap only
    /// (`.privileged`), and it must be `codes.pod.rootd` signed by Pod's team.
    public convenience init(
        appIdentifier: String = PodRootd.appIdentifier, team: String = PodRootd.teamIdentifier,
        onCancel: (@Sendable (XPCRichError) -> Void)? = nil
    ) throws {
        let session = try XPCSession(
            machService: PodRootd.serviceName(appIdentifier: appIdentifier), options: .privileged,
            requirement: try Self.helperRequirement(team: team), cancellationHandler: onCancel)
        self.init(session: session)
    }

    /// A helper behind an endpoint (tests: an anonymous listener in the same process).
    public convenience init(endpoint: XPCEndpoint) throws {
        self.init(session: try XPCSession(endpoint: endpoint))
    }

    private init(session: XPCSession) { self.session = session }

    /// libxpc crashes on the last release of an active session that wasn't cancelled
    /// (`xpc/session.h`), so letting go of a client ends its session, and its leases, like
    /// `close()`. A second cancel is harmless.
    deinit {
        session.cancel(reason: "client released")
    }

    /// What a client asks of the service it reaches.
    public static func helperRequirement(team: String = PodRootd.teamIdentifier) throws -> XPCPeerRequirement {
        .codeRequirement(try ProcessCodeRequirement.allOf {
            TeamIdentifier(team)
            SigningIdentifier(PodRootd.helperIdentifier)
        })
    }

    /// One verb; a refusal comes back in the reply, not as an error. Errors are the transport's
    /// (helper not running, not approved, not ours).
    public func send(_ verb: Verb) async throws -> Reply {
        try await send(verb, approval: nil)
    }

    /// With the CLI's approval for a tier B verb; from any other caller the helper ignores it.
    public func send(_ verb: Verb, approval: Approval?) async throws -> Reply {
        let request = Request(verb, approval: approval)
        return try await withCheckedThrowingContinuation { continuation in
            do {
                try session.send(request) { (result: Result<Reply, any Error>) in
                    continuation.resume(with: result)
                }
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }

    /// Like `send`, but a refusal throws it.
    @discardableResult
    public func run(_ verb: Verb) async throws -> Reply {
        let reply = try await send(verb)
        if let refusal = reply.refusal { throw refusal }
        return reply
    }

    public func status() async throws -> Status {
        guard let status = try await run(.status).status else { throw Refusal.failed("the helper sent no status") }
        return status
    }

    /// Ends the session, and with it this client's leases.
    public func close() {
        session.cancel(reason: "client closed")
    }
}
