import AccKit
import Foundation
import Observation
import PodRootdClient
import SMCKit

/// Pod's root helper (docs/pod-rootd.md) as Pod Menu sees it: one XPC session for the app's life,
/// the fan mode and the lid hold through it, and the fan readings from the SMC itself (reading needs
/// no root). It takes over from fanctl's files only once the helper answers and the old fans daemon
/// is migrated; until then fans.json, fans-state.json and awake.json stay in charge.
@Observable
final class RootHelper {
    static let shared = RootHelper(appIdentifier: hostAppIdentifier())

    /// The helper answers and the old fans daemon is gone: the fans and the lid are the helper's.
    private(set) var owns = false
    private(set) var status: Status?
    /// Readings in the shape of fanctl's state file, for the Load & Heat card and the menu bar.
    private(set) var fanState: FanState?

    @ObservationIgnored private let appIdentifier: String?
    /// Opens the session in tests (an anonymous listener); nil: the helper's Mach service.
    @ObservationIgnored private let connector: (() throws -> PodRootdClient)?
    @ObservationIgnored private var client: PodRootdClient?
    @ObservationIgnored private var ticker: Task<Void, Never>?
    @ObservationIgnored private var fans: Fans?
    @ObservationIgnored private var history: [[Double]] = []
    @ObservationIgnored private var statusAt = -Double.infinity
    /// The lid hold Awake asks for (nil: none), and when the helper last got it.
    @ObservationIgnored private var lidWanted: Date?
    @ObservationIgnored private var lidSentAt: Date?
    @ObservationIgnored private var lidSentFor: Date?
    /// A lid sync is running; another asked for meanwhile runs right after it.
    @ObservationIgnored private var lidBusy = false
    @ObservationIgnored private var lidAgain = false
    /// Failed status reads in a row, and when the next may go. The helper is opt-in, so on most Macs
    /// nothing answers, and a new XPC session every 5 s forever would be waste.
    @ObservationIgnored private var failures = 0
    @ObservationIgnored private var retryAt = -Double.infinity
    @ObservationIgnored var panelOpen = false {
        didSet { if panelOpen, !oldValue { retryAt = -.infinity } }  // opening the panel tries again now
    }

    /// A hold is renewed this often, so a hold without an end outlasts the helper's 24 h cap.
    private static let lidRenew: TimeInterval = 30 * 60
    private static let sampleEvery: Duration = .seconds(5)

    init(appIdentifier: String?, connector: (() throws -> PodRootdClient)? = nil, ticking: Bool = true) {
        self.appIdentifier = appIdentifier
        self.connector = connector
        guard appIdentifier != nil || connector != nil, ticking else { return }
        fans = (try? SMC()).map(Fans.init)
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: Self.sampleEvery)
            }
        }
    }

    /// Pod's bundle id when this app is Pod Menu (Pod.app/Contents/Library/LoginItems/Pod Menu.app);
    /// the standalone Claude Acc.app has no helper.
    static func hostAppIdentifier() -> String? {
        guard PodMenu.active else { return nil }
        let app = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return Bundle(url: app)?.bundleIdentifier
    }

    // MARK: Asks

    /// Fan mode as the card picks it: "auto" or a percent.
    func setFans(_ mode: String) async -> String? {
        let wanted: FanMode
        if mode == "auto" {
            wanted = .auto
        } else if let percent = Int(mode).flatMap({ FanPercent($0) }) {
            wanted = .fixed(percent)
        } else {
            return "\(mode) is not auto or 30-100%"
        }
        guard let client = connect() else { return "Pod's root helper doesn't answer" }
        do {
            let reply = try await client.send(.fansSet(mode: wanted))
            if let status = reply.status { take(status) }
            return reply.refusal.map { "\($0)" }
        } catch {
            dropped()
            return "Pod's root helper doesn't answer: \(error.localizedDescription)"
        }
    }

    /// What Awake wants for the lid, every time it decides; the helper gets it while it owns the lid.
    func wantLid(until: Date?) {
        guard until != lidWanted else { return }
        lidWanted = until
        // a hold asked for during a long backoff is tried at once
        if until != nil { retryAt = -.infinity }
        Task { await syncLid() }
    }

    /// One verb for AccKit's views (PodMenuRootHelper): the reply's status taken, a refusal thrown.
    func send(_ verb: Verb) async throws(AccRootError) {
        guard let client = connect() else { throw .unreachable(Self.unreachable) }
        let reply: Reply
        do {
            reply = try await client.send(verb)
        } catch {
            dropped()
            throw .unreachable("\(Self.unreachable): \(error.localizedDescription)")
        }
        if let status = reply.status { take(status) }
        if let refusal = reply.refusal { throw Self.accError(refusal) }
    }

    /// AccKit's lid hold, nil releasing it: the same hold Awake asks for, sent now. While the old
    /// fans daemon is installed it drives the lid, so the views are told to migrate first.
    func holdLid(until: Date?) async throws(AccRootError) {
        guard connect() != nil else { throw .unreachable(Self.unreachable) }
        if !owns { await refreshStatus() }
        guard client != nil else { throw .unreachable(Self.unreachable) }
        guard owns else {
            if until == nil { lidWanted = nil; return }
            throw .refused("the old fans daemon still drives the lid: run pod-rootctl legacy migrate first", retryAfter: nil)
        }
        lidWanted = until
        if let problem = await syncLid() { throw problem }
    }

    // MARK: Loop

    func tick() async {
        // the helper's status every 5 s with the panel open, every minute otherwise (and at once
        // when the lid hold is due); after failed reads only on the backoff
        let now = Date.now.timeIntervalSince1970
        guard now >= retryAt else { return }
        if panelOpen || now - statusAt >= 60 || status == nil {
            await refreshStatus()
        }
        if owns { sample() }
        await syncLid()
    }

    private func refreshStatus() async {
        guard let client = connect() else {
            set(owns: false)
            return failed()
        }
        do {
            take(try await client.status())
        } catch {
            dropped()
            failed()
        }
    }

    /// The wait after `failures` failed reads in a row: the first retry at the next tick, then 5 s
    /// doubling up to 5 minutes.
    static func backoff(after failures: Int) -> TimeInterval {
        failures <= 1 ? 0 : min(5 * pow(2, Double(failures - 2)), 300)
    }

    private func failed() {
        failures += 1
        let now = Date.now
        retryAt = now.timeIntervalSince1970 + Self.retryDelay(after: failures, lidWanted: lidWanted, now: now)
    }

    /// While Stay Awake wants the lid held, the wait never passes 5 s: the helper keeps a dropped
    /// hold for a minute (Engine.lidRehold), and a longer wait could miss it.
    static func retryDelay(after failures: Int, lidWanted: Date?, now: Date) -> TimeInterval {
        let wait = backoff(after: failures)
        guard let until = lidWanted, until > now else { return wait }
        return min(wait, 5)
    }

    private func take(_ fresh: Status) {
        failures = 0
        retryAt = -.infinity
        statusAt = Date.now.timeIntervalSince1970
        if fresh != status { status = fresh }
        set(owns: Self.owns(fresh))
    }

    /// While the old fans daemon is still in /Library/LaunchDaemons it follows fans.json and
    /// awake.json; two drivers would fight over the SMC and SleepDisabled.
    static func owns(_ status: Status) -> Bool {
        !(status.legacy.first { $0.daemon == .fans }?.installed ?? false)
    }

    private func set(owns value: Bool) {
        let lost = owns && !value
        if value != owns { owns = value }
        if !value, fanState != nil { fanState = nil }
        // the old fans daemon is back (a rollback): our hold would outlive the flip, because the
        // session stays up and so does its lease
        if lost, lidSentAt != nil, let client {
            lidSentAt = nil
            lidSentFor = nil
            Task { _ = try? await client.send(.lidRelease) }
        }
    }

    private func connect() -> PodRootdClient? {
        if let client { return client }
        if let connector {
            client = try? connector()
        } else if let appIdentifier {
            client = try? PodRootdClient(appIdentifier: appIdentifier) { [weak self] _ in
                Task { @MainActor in self?.dropped() }
            }
        }
        return client
    }

    /// The session ended (the helper restarted, was switched off, or never ran): leases went with
    /// it. The next tick reads the status again and holds the lid again, within the minute the
    /// helper keeps a dropped hold.
    func dropped() {
        client = nil
        lidSentAt = nil
        statusAt = -.infinity
        set(owns: false)
    }

    /// One sync at a time: wantLid's task and the tick both ask, and two interleaved at an await
    /// would each act on the other's half-done state. A call during one runs right after it and
    /// answers nil: the running one reports.
    @discardableResult
    private func syncLid() async -> AccRootError? {
        guard !lidBusy else {
            lidAgain = true
            return nil
        }
        lidBusy = true
        defer { lidBusy = false }
        var problem: AccRootError?
        repeat {
            lidAgain = false
            problem = await syncLidOnce()
        } while lidAgain
        return problem
    }

    /// The hold renews before it runs out, and again after a reconnect; nil releases it. A refused
    /// hold (a rate limit) is tried again at the next tick.
    private func syncLidOnce() async -> AccRootError? {
        guard owns, let client = connect() else { return nil }
        let now = Date.now
        if let until = lidWanted, until > now {
            let fresh = lidSentFor == until && lidSentAt.map { now.timeIntervalSince($0) < Self.lidRenew } == true
            let leased = status?.lid.leaseUntil != nil
            guard !fresh || !leased else { return nil }
            let seconds = Int64(min(max(until.timeIntervalSince(now), 60), 86_400))
            guard let reply = try? await client.send(.lidHold(seconds: LidSeconds(seconds) ?? LidSeconds(unchecked: 60))) else {
                dropped()
                return .unreachable(Self.unreachable)
            }
            if let status = reply.status { take(status) }
            if let refusal = reply.refusal { return Self.accError(refusal) }
            lidSentAt = now
            lidSentFor = until
        } else if lidSentAt != nil || status?.lid.leaseUntil != nil {
            guard let reply = try? await client.send(.lidRelease) else {
                dropped()
                return .unreachable(Self.unreachable)
            }
            if let status = reply.status { take(status) }
            if let refusal = reply.refusal { return Self.accError(refusal) }
            lidSentAt = nil
            lidSentFor = nil
        }
        return nil
    }

    /// One reading of the fans and the hottest sensors, plus fanctl's 20-minute history.
    private func sample() {
        guard let fans else { return }
        let reading = fans.reading()
        let rpm = reading.fans.isEmpty ? 0 : reading.fans.map(\.rpm).reduce(0, +) / Double(reading.fans.count)
        let tenth = { (value: Double?) in ((value ?? 0) * 10).rounded() / 10 }
        history.append([reading.at.rounded(), tenth(reading.cpu), tenth(reading.gpu), rpm.rounded()])
        if history.count > 240 { history.removeFirst(history.count - 240) }
        let helper = status?.fans
        var mode = "auto"
        var percent: Int?
        if case .fixed(let value)? = helper?.mode {
            mode = "fixed"
            percent = Int(value.value)
        }
        fanState = FanState(
            at: reading.at,
            fans: reading.fans.map {
                FanState.Fan(index: $0.index, rpm: $0.rpm, min: $0.min, max: $0.max, target: $0.target, manual: $0.manual)
            },
            cpu: reading.cpu, gpu: reading.gpu, mode: mode, percent: percent,
            boosting: helper?.boosting, conflict: helper?.conflict, sensors: reading.sensors, history: history,
            error: helper?.error)
    }
}
