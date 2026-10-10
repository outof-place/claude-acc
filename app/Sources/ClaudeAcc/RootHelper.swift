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
    @ObservationIgnored private var client: PodRootdClient?
    @ObservationIgnored private var ticker: Task<Void, Never>?
    @ObservationIgnored private var fans: Fans?
    @ObservationIgnored private var history: [[Double]] = []
    @ObservationIgnored private var statusAt = -Double.infinity
    /// The lid hold Awake asks for (nil: none), and when the helper last got it.
    @ObservationIgnored private var lidWanted: Date?
    @ObservationIgnored private var lidSentAt: Date?
    @ObservationIgnored private var lidSentFor: Date?
    @ObservationIgnored var panelOpen = false

    /// A hold is renewed this often, so a hold without an end outlasts the helper's 24 h cap.
    private static let lidRenew: TimeInterval = 30 * 60
    private static let sampleEvery: Duration = .seconds(5)

    init(appIdentifier: String?) {
        self.appIdentifier = appIdentifier
        guard appIdentifier != nil else { return }
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
        let wanted: FanMode = mode == "auto" ? .auto : .fixed(FanPercent(Int(mode) ?? 100) ?? FanPercent(unchecked: 100))
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
        Task { await syncLid() }
    }

    // MARK: Loop

    private func tick() async {
        // the helper's status every 5 s with the panel open, every minute otherwise (and at once
        // when the lid hold is due)
        let now = Date.now.timeIntervalSince1970
        if panelOpen || now - statusAt >= 60 || status == nil {
            await refreshStatus()
        }
        if owns { sample() }
        await syncLid()
    }

    private func refreshStatus() async {
        guard let client = connect() else { return set(owns: false) }
        do {
            take(try await client.status())
        } catch {
            dropped()
        }
    }

    private func take(_ fresh: Status) {
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
        if value != owns { owns = value }
        if !value, fanState != nil { fanState = nil }
    }

    private func connect() -> PodRootdClient? {
        if let client { return client }
        guard let appIdentifier else { return nil }
        client = try? PodRootdClient(appIdentifier: appIdentifier) { [weak self] _ in
            Task { @MainActor in self?.dropped() }
        }
        return client
    }

    /// The session ended (the helper restarted, was switched off, or never ran): leases went with it.
    private func dropped() {
        client = nil
        lidSentAt = nil
        set(owns: false)
    }

    /// The hold renews before it runs out, and again after a reconnect; nil releases it.
    private func syncLid() async {
        guard owns, let client = connect() else { return }
        let now = Date.now
        if let until = lidWanted, until > now {
            let fresh = lidSentFor == until && lidSentAt.map { now.timeIntervalSince($0) < Self.lidRenew } == true
            let leased = status?.lid.leaseUntil != nil
            guard !fresh || !leased else { return }
            let seconds = Int64(min(max(until.timeIntervalSince(now), 60), 86_400))
            guard let reply = try? await client.send(.lidHold(seconds: LidSeconds(seconds) ?? LidSeconds(unchecked: 60))) else {
                return dropped()
            }
            lidSentAt = now
            lidSentFor = until
            if let status = reply.status { take(status) }
        } else if lidSentAt != nil || status?.lid.leaseUntil != nil {
            if let reply = try? await client.send(.lidRelease), let status = reply.status { take(status) }
            lidSentAt = nil
            lidSentFor = nil
        }
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
