import Foundation
@testable import PodRootdCore
import PodRootdProtocol

/// A clock the tests move by hand.
final class Clock {
    var now: Double = 1_791_600_000

    func advance(_ seconds: Double) { now += seconds }
}

/// An engine on the fake machine, started as launchd would start it.
struct Rig {
    let backend: FakeBackend
    let store: MemoryStateStore
    let clock: Clock
    let engine: Engine

    let updates: Bool

    init(
        backend: FakeBackend = FakeBackend(), store: MemoryStateStore = MemoryStateStore(), clock: Clock = Clock(),
        limits: [VerbKind: RateLimiter.Limit] = [:], updates: Bool = false
    ) {
        self.backend = backend
        self.store = store
        self.clock = clock
        self.updates = updates
        engine = Engine(backend: backend, store: store, now: { clock.now }, limits: limits, updates: updates)
        engine.start()
    }

    /// The same machine and state file after the helper restarted (a crash, an idle exit, an update).
    func restarted() -> Rig { Rig(backend: backend, store: store, clock: clock, updates: updates) }

    @discardableResult
    func send(_ verb: Verb, as caller: Caller = .menu, session: SessionID = SessionID(1)) -> Reply {
        engine.handle(Request(verb), from: caller, session: session)
    }

    func changed(_ reply: Reply) -> Bool? {
        if case .done(let changed, _, _) = reply.outcome { changed } else { nil }
    }
}

let fan50 = FanMode.fixed(FanPercent(unchecked: 50))
let en0 = InterfaceName("en0")!
