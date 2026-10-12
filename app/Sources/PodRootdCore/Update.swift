import Foundation

/// What a self-update reads from Pod.app (docs/pod-rootd.md, "Updates"). It runs on its own queue:
/// Pod.app belongs to the user, so a candidate can be made to block a read (a FIFO, a stalled mount),
/// and the signature check may ask Apple's notary service. The main queue, which serves XPC, the
/// lid, heat and SIGTERM, never waits for it.
nonisolated public protocol UpdateSource: Sendable {
    /// Copies a candidate into the root-only directory next to the installed helper; the copy's path.
    func stage(_ candidate: String) throws -> String
    /// The staged copy's signed CFBundleVersion when it is Pod's notarized helper; nil otherwise.
    func verifiedVersion(ofStaged path: String) -> String?
    func discard(_ staged: String)
}

/// The newest genuine candidate one look found, staged and checked.
nonisolated public struct UpdateFound: Sendable, Equatable {
    public var candidate: String
    public var staged: String
    public var version: String
}

/// One look: what it found, and why the other candidates were passed over.
nonisolated public struct UpdateLook: Sendable, Equatable {
    public var found: UpdateFound?
    public var problems: [String] = []
}

/// Runs a look on a utility queue and hands the result to the main queue, or nil once `deadline`
/// passes. A look that comes back late has its staged copy thrown away: a timeout means "skip".
nonisolated public struct UpdateRunner: Sendable {
    public var deadline: Double

    public init(deadline: Double) { self.deadline = deadline }

    public static let standard = UpdateRunner(deadline: 10)

    public func look(
        _ candidates: [String], newerThan own: String, in source: any UpdateSource,
        done: @escaping @MainActor @Sendable (UpdateLook?) -> Void
    ) {
        let answered = Answered()
        DispatchQueue.global(qos: .utility).async {
            let look = Self.look(candidates, newerThan: own, in: source)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    if answered.claim() {
                        done(look)
                    } else if let found = look.found {
                        source.discard(found.staged)
                    }
                }
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + deadline) {
            MainActor.assumeIsolated {
                if answered.claim() { done(nil) }
            }
        }
    }

    /// The work itself, off the main queue: the first candidate that is genuine and newer.
    static func look(_ candidates: [String], newerThan own: String, in source: any UpdateSource) -> UpdateLook {
        var look = UpdateLook()
        for candidate in candidates {
            let staged: String
            do {
                staged = try source.stage(candidate)
            } catch {
                look.problems.append("\(candidate): \(error)")
                continue
            }
            guard let version = source.verifiedVersion(ofStaged: staged), Engine.newer(version, than: own) else {
                source.discard(staged)
                continue
            }
            look.found = UpdateFound(candidate: candidate, staged: staged, version: version)
            return look
        }
        return look
    }
}

/// Which of the two main-queue blocks came first; only ever touched on the main queue.
nonisolated private final class Answered: @unchecked Sendable {
    private var done = false

    func claim() -> Bool {
        defer { done = true }
        return !done
    }
}
