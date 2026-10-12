/// The helper's answer to every request: what happened and, unless the caller was refused before
/// the verb, the state after it.
public struct Reply: Codable, Hashable, Sendable {
    public var outcome: Outcome
    public var status: Status?

    public init(_ outcome: Outcome, status: Status? = nil) {
        self.outcome = outcome
        self.status = status
    }

    public var refusal: Refusal? {
        if case .refused(let refusal) = outcome { refusal } else { nil }
    }
}

public enum Outcome: Codable, Hashable, Sendable {
    /// `changed: false` when the wanted state was already there.
    case done(changed: Bool, note: String?, report: Report?)
    case refused(Refusal)
}

/// Extra results of the verbs that find things.
public enum Report: Codable, Hashable, Sendable {
    case orphans([Orphan], parked: Bool)
    case pruned(files: Int, bytes: Int64, dryRun: Bool)
    case legacy([LegacyDaemon])
}

/// A launchd plist in /Library whose program is gone (janitor-root.sh's case).
public struct Orphan: Codable, Hashable, Sendable {
    public var label: String
    public var plist: String
    public var program: String
    /// `system` for LaunchDaemons, `gui/<uid>` for LaunchAgents.
    public var domain: String

    public init(label: String, plist: String, program: String, domain: String) {
        self.label = label
        self.plist = plist
        self.program = program
        self.domain = domain
    }
}

public enum Refusal: Error, Codable, Hashable, Sendable, CustomStringConvertible {
    /// The sender matched none of the allowed signing identities.
    case peerNotAllowed
    case verbNotAllowed(verb: String, caller: Caller)
    /// A tier B verb from the CLI without a fresh approval: authenticate and send it again.
    case needsApproval(verb: String)
    case rateLimited(retryAfter: Double)
    /// The request did not decode, or a parameter is outside what this Mac allows.
    case invalid(String)
    case versionMismatch(helper: Int)
    /// This Mac can't do it (no high power mode, no such interface).
    case unsupported(String)
    /// The system call or tool failed; the state says how far it got.
    case failed(String)

    public var description: String {
        switch self {
        case .peerNotAllowed: "caller not allowed"
        case .verbNotAllowed(let verb, let caller): "\(verb) is not allowed for \(caller.rawValue)"
        case .needsApproval(let verb): "\(verb) needs your approval (Touch ID)"
        case .rateLimited(let after): "rate limited, retry in \(Int(after.rounded(.up))) s"
        case .invalid(let why): "invalid: \(why)"
        case .versionMismatch(let helper): "protocol version mismatch (helper speaks \(helper))"
        case .unsupported(let why): "unsupported: \(why)"
        case .failed(let why): "failed: \(why)"
        }
    }
}
