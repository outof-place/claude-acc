import LightweightCodeRequirements
import PodRootdProtocol
import XPC

/// Who may send which tier (docs/pod-rootd.md, "Tiers"). Pod's Electron process runs its helpers
/// with ELECTRON_RUN_AS_NODE, so the RunAsNode fuse stays on and any local process can run Pod's
/// signed binary as Node: it gets tier A only. `pod-rootctl` can be run by anything, so its tier B
/// needs an approval; Pod Menu's tier B comes from a click.
public struct VerbPolicy: Sendable {
    public var allowed: [VerbTier: Set<Caller>]
    /// Callers whose tier B needs an `Approval`: authenticated now, or within `grace` for the same parent.
    public var approvalNeeded: Set<Caller>
    public var grace: Double

    public init(allowed: [VerbTier: Set<Caller>], approvalNeeded: Set<Caller>, grace: Double) {
        self.allowed = allowed
        self.approvalNeeded = approvalNeeded
        self.grace = grace
    }

    public static let standard = VerbPolicy(
        allowed: [.a: [.app, .menu, .cli], .b: [.menu, .cli]], approvalNeeded: [.cli], grace: 5 * 60)

    public func permits(_ tier: VerbTier, for caller: Caller) -> Bool {
        allowed[tier]?.contains(caller) ?? false
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
