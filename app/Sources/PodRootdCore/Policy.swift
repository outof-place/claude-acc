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
    /// A sender that satisfies any of these is refused whatever else it matches. Lightweight code
    /// requirements can't say "not", so the listener can't keep a debuggable build out; each
    /// message is tested against these instead.
    public var refused: [XPCPeerRequirement]

    public init(listener: XPCPeerRequirement, callers: [(Caller, XPCPeerRequirement)], refused: [XPCPeerRequirement] = []) {
        self.listener = listener
        self.callers = callers
        self.refused = refused
    }

    /// Team `PodRootd.teamIdentifier`, Developer ID, the hardened runtime, one of the three signing
    /// identifiers; Pod Menu and pod-rootctl also with library validation (`codesign --options
    /// runtime,library`), so nothing can be injected into a tier B caller. An Apple Development or
    /// non-hardened copy with the right team and identifier is turned away (docs/pod-rootd.md,
    /// "Signing").
    public static func production(team: String = PodRootd.teamIdentifier) throws -> PeerPolicy {
        let listener = try requirement(
            team: team, identifiers: Caller.allCases.map(\.signingIdentifier), flags: [.isHardenedRuntimeEnforced])
        let callers = try Caller.allCases.map { caller in
            (caller, try requirement(team: team, identifiers: [caller.signingIdentifier], flags: hardening(caller)))
        }
        return PeerPolicy(listener: listener, callers: callers, refused: try debuggable())
    }

    /// A build with `get-task-allow` (`CS_GET_TASK_ALLOW`, LWCR's `isDebuggable`: measured, a binary
    /// with the entitlement runs under a launch requirement of it, one without is killed) or a
    /// process a debugger is attached to (`CS_DEBUGGED`). Pod's terminals have Developer Tools access
    /// (Ultra's devtools tweak), so anything they run could take over such a caller with
    /// `task_for_pid`.
    /// One requirement per flag: a requirement can't hold the same constraint twice.
    public static func debuggable() throws -> [XPCPeerRequirement] {
        try [ProcessCodeSigningFlags.ValueSet.isDebuggable, .isDebugged].map { flag in
            .codeRequirement(try ProcessCodeRequirement.allOf { ProcessCodeSigningFlags.isSuperset(of: [flag]) })
        }
    }

    /// The code signing flags a running caller must have. Pod's Electron process loads native
    /// modules and gets tier A only, so it needs the hardened runtime but not library validation.
    public static func hardening(_ caller: Caller) -> ProcessCodeSigningFlags.ValueSet {
        caller == .app ? [.isHardenedRuntimeEnforced] : [.isHardenedRuntimeEnforced, .isLibraryValidationRequired]
    }

    /// One requirement: the identity, and the flags the running process must carry. `team` nil
    /// leaves out the team and the Developer ID category, for tests whose runner is ad hoc signed.
    static func requirement(
        team: String?, identifiers: [String], flags: ProcessCodeSigningFlags.ValueSet
    ) throws -> XPCPeerRequirement {
        .codeRequirement(try ProcessCodeRequirement.allOf {
            if let team {
                TeamIdentifier(team)
                ValidationCategory(.developerID)
            }
            SigningIdentifier.in(identifiers)
            ProcessCodeSigningFlags.isSuperset(of: flags)
        })
    }

    /// The caller a message comes from, or nil when it matches none (it never should: the listener
    /// requirement already let it in) or matches `refused`.
    public func classify(_ satisfies: (XPCPeerRequirement) -> Bool) -> Caller? {
        if refused.contains(where: satisfies) { return nil }
        return callers.first { satisfies($0.1) }?.0
    }
}
