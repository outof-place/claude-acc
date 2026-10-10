import LightweightCodeRequirements
import PodRootdProtocol
import XPC

/// Who may send which kind of verb. System verbs go only to Pod Menu and the CLI: the CLI asks for
/// Touch ID before one, and the Electron main process stays off them until its Node fuses are known
/// to be off (docs/pod-rootd.md, "Signing").
public struct VerbPolicy: Sendable {
    public var allowed: [VerbKind: Set<Caller>]

    public init(allowed: [VerbKind: Set<Caller>]) { self.allowed = allowed }

    public static let standard = VerbPolicy(allowed: Dictionary(uniqueKeysWithValues: VerbKind.allCases.map {
        ($0, $0 == .system ? [.menu, .cli] : Set(Caller.allCases))
    }))

    public func permits(_ kind: VerbKind, for caller: Caller) -> Bool {
        allowed[kind]?.contains(caller) ?? false
    }
}

/// The code signing side: the listener's requirement (checked by the system for every message,
/// before any handler runs) and one requirement per caller, which each message is tested against
/// with `senderSatisfies` to name the caller.
nonisolated public struct PeerPolicy: Sendable {
    public var listener: XPCPeerRequirement
    public var callers: [(Caller, XPCPeerRequirement)]

    public init(listener: XPCPeerRequirement, callers: [(Caller, XPCPeerRequirement)]) {
        self.listener = listener
        self.callers = callers
    }

    /// Team `PodRootd.teamIdentifier` with one of the three signing identifiers.
    public static func production(team: String = PodRootd.teamIdentifier) throws -> PeerPolicy {
        let ids = Caller.allCases.map(\.signingIdentifier)
        let listener = try ProcessCodeRequirement.allOf {
            TeamIdentifier(team)
            SigningIdentifier.in(ids)
        }
        let callers = try Caller.allCases.map { caller in
            (caller, XPCPeerRequirement.codeRequirement(try ProcessCodeRequirement.allOf {
                TeamIdentifier(team)
                SigningIdentifier(caller.signingIdentifier)
            }))
        }
        return PeerPolicy(listener: .codeRequirement(listener), callers: callers)
    }

    /// The caller a message comes from, or nil when it matches none (it never should: the listener
    /// requirement already let it in).
    public func classify(_ satisfies: (XPCPeerRequirement) -> Bool) -> Caller? {
        callers.first { satisfies($0.1) }?.0
    }
}
