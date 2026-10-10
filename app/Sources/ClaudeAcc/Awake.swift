import Foundation
import IOKit.pwr_mgt
import Network
import Observation

/// Keeps the Mac awake, like Amphetamine: by hand, for a while, or on its own whenever
/// the Mac is on a hotspot, and then keeps that hotspot connection from dozing off.
@Observable
final class Awake {
    /// Manual session: until a date, or `.distantFuture` for "until I turn it off".
    private(set) var manualUntil: Date?
    /// The default route goes through the phone, by the rule tether-profile acts on (`perf.py
    /// link`). It used to be the path's "expensive" flag: a second rule, and the two disagreed.
    private(set) var onHotspot = false
    /// The hardware port the hotspot comes through: "iPhone USB", "Wi-Fi", "Bluetooth PAN".
    private(set) var hotspotVia: String?
    @ObservationIgnored private(set) var lastKeepAlive: Date?
    private(set) var keepAliveFailing = false
    /// Turned off by hand while on a hotspot: stays off until the next hotspot.
    private(set) var hotspotDismissed = false

    var autoOnHotspot: Bool { didSet { save(); apply() } }
    var keepDisplayOn: Bool { didSet { save(); apply() } }
    var keepHotspotAlive: Bool { didSet { save() } }
    /// A session started by hand also survives closing the lid, through the fan daemon.
    var lidClosed: Bool { didSet { save(); apply() } }

    var manualActive: Bool { manualUntil.map { $0 > .now } ?? false }
    var hotspotActive: Bool { autoOnHotspot && onHotspot && !hotspotDismissed }
    var isOn: Bool { manualActive || hotspotActive }

    @ObservationIgnored private var systemAssertion: IOPMAssertionID = 0
    @ObservationIgnored private var displayAssertion: IOPMAssertionID = 0
    @ObservationIgnored private let monitor = NWPathMonitor()
    @ObservationIgnored private var ticker: Task<Void, Never>?
    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private let preview: Bool
    /// The `until` last sent to the daemon, 0 for no request; nil before the first write.
    @ObservationIgnored private var lidRequested: Double?
    @ObservationIgnored private var linkChecking = false
    /// The path changed again during a check: one more check follows it.
    @ObservationIgnored private var linkChanged = false
    /// The last check got no answer (perf.py missing or failing): the next tick asks again.
    @ObservationIgnored private var linkUnknown = false
    /// The state file's last contents without its timestamp: rewritten only when they change.
    @ObservationIgnored private var publishedState: [String: AnyHashable]?

    private static let probe = URL(string: "http://captive.apple.com/hotspot-detect.html")!
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 6
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    /// `link`: a panel render shows the route `perf keep` last saw, without acting on it.
    init(preview: Bool = false, link: TetherLink? = nil) {
        autoOnHotspot = defaults.object(forKey: "awake.autoOnHotspot") as? Bool ?? true
        keepDisplayOn = defaults.bool(forKey: "awake.keepDisplayOn")
        keepHotspotAlive = defaults.object(forKey: "awake.keepHotspotAlive") as? Bool ?? true
        lidClosed = defaults.object(forKey: "awake.lidClosed") as? Bool ?? true
        self.preview = preview
        let until = defaults.double(forKey: "awake.until")
        manualUntil = until > 0 ? Date(timeIntervalSince1970: until) : nil
        guard !preview else {
            if let link, link.tethered {
                onHotspot = true
                hotspotVia = link.via
            }
            return
        }
        // the monitor only says when the route may have changed; perf.py says where it goes
        monitor.pathUpdateHandler = { [weak self] _ in
            Task { @MainActor in self?.checkLink() }
        }
        monitor.start(queue: DispatchQueue(label: "claude-acc.awake.path"))
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: .seconds(25))
            }
        }
        apply()
    }

    // MARK: Control

    /// `nil` keeps the Mac awake until it is turned off.
    func stayAwake(for duration: TimeInterval?) {
        manualUntil = duration.map { Date.now.addingTimeInterval($0) } ?? .distantFuture
        save()
        apply()
    }

    func turnOff() {
        manualUntil = nil
        if hotspotActive { hotspotDismissed = true }
        save()
        apply()
    }

    /// `claude-acc awake` opens claude-acc://awake/<on|off|toggle>[?for=<seconds>] in the background.
    func command(_ url: URL) {
        let seconds = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .first { $0.name == "for" }?.value.flatMap(TimeInterval.init).flatMap { $0 > 0 ? $0 : nil }
        switch url.lastPathComponent {
        case "on": stayAwake(for: seconds)
        case "off": turnOff()
        default: isOn ? turnOff() : stayAwake(for: seconds)
        }
    }

    /// `claude-acc awake lid on|off` opens claude-acc://awake-lid/<on|off> in the background: the
    /// "awake with the lid closed" setting, for Pod's native window. A host of its own, so an app
    /// from before it ignores the URL instead of reading `on` as a plain Stay Awake.
    func lidCommand(_ url: URL) {
        switch url.lastPathComponent {
        case "on": lidClosed = true
        case "off": lidClosed = false
        // an unknown or empty verb never changes whether a closed Mac stays up
        default: return
        }
    }

    // MARK: Internals

    /// Asks perf.py where the default route goes (about 0.4 s, off the main actor); path changes
    /// that come in the meantime fold into one more check.
    private func checkLink() {
        guard !linkChecking else {
            linkChanged = true
            return
        }
        linkChecking = true
        linkChanged = false
        Task {
            let result = await CLI.run(["link", "--json"], script: CLI.perf)
            let link = result.status == 0 ? Store.decode(TetherLink.self, from: Data(result.stdout.utf8)) : nil
            linkChecking = false
            linkUnknown = link == nil
            if let link { pathChanged(hotspot: link.tethered, via: link.via) }
            if linkChanged { checkLink() }
        }
    }

    private func pathChanged(hotspot: Bool, via: String?) {
        if !hotspot { hotspotDismissed = false }
        let joined = hotspot && !onHotspot
        onHotspot = hotspot
        hotspotVia = hotspot ? via : nil
        apply()
        if joined { Task { await keepAlive() } }
    }

    private func tick() async {
        if let until = manualUntil, until <= .now {
            manualUntil = nil
            save()
        }
        if linkUnknown { checkLink() }
        apply()
        if onHotspot && keepHotspotAlive && isOn {
            await keepAlive()
        }
    }

    /// A tiny request through the hotspot: the phone sees a client in use and keeps tethering
    /// on, and the NAT on the way keeps the flows of long-running sessions mapped.
    private func keepAlive() async {
        var request = URLRequest(url: Self.probe)
        request.httpMethod = "HEAD"
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let ok: Bool
        do {
            let (_, response) = try await Self.session.data(for: request)
            ok = (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            ok = false
        }
        // the card reads the time on its own 5 s clock: no need to wake it every 25 s
        lastKeepAlive = .now
        if keepAliveFailing == ok { keepAliveFailing = !ok }
    }

    /// Power assertions follow the state: system sleep always, the display only on request.
    private func apply() {
        set(&systemAssertion, type: "PreventUserIdleSystemSleep", on: isOn)
        set(&displayAssertion, type: "PreventUserIdleDisplaySleep", on: isOn && keepDisplayOn)
        requestLid()
        publishState()
    }

    /// What the menu bar shows, for `claude-acc awake status` and the Orca plugin. Only this pid
    /// holds the assertions, so a reader checks it is still alive before trusting `on`.
    private func publishState() {
        guard !preview else { return }
        let forever = manualUntil == .distantFuture
        var state: [String: AnyHashable] = [
            "on": isOn,
            "manual": manualActive,
            "forever": manualActive && forever,
            "hotspot": hotspotActive,
            "on_hotspot": onHotspot,
            "auto_on_hotspot": autoOnHotspot,
            "keep_display": keepDisplayOn,
            "lid_closed": lidClosed,
            // `lidCommand`: readers offer the lid setting only to an app that takes it
            "lid_settable": true,
            "pid": Int(getpid()),
        ]
        if manualActive, !forever, let until = manualUntil { state["until"] = until.timeIntervalSince1970 }
        if let hotspotVia { state["via"] = hotspotVia }
        guard state != publishedState else { return }
        var file = state
        file["updated_at"] = Date.now.timeIntervalSince1970
        guard let data = try? JSONSerialization.data(withJSONObject: file, options: [.sortedKeys]),
              (try? data.write(to: URL(fileURLWithPath: CLI.awakeState), options: .atomic)) != nil
        else { return }
        publishedState = state
    }

    /// An assertion stops idle sleep only: a closed lid sleeps a MacBook on battery anyway. The fan
    /// daemon (root) holds `SleepDisabled` while this file asks for it, and only as long as this
    /// pid runs. A session that started itself on a hotspot doesn't ask: a Mac awake in a bag has
    /// to be a choice. Written when the request changes, not on every tick.
    private func requestLid() {
        guard !preview else { return }
        let until = manualActive && lidClosed ? manualUntil?.timeIntervalSince1970 ?? 0 : 0
        // in Pod the root helper holds it on this app's session, once the old fans daemon is migrated
        RootHelper.shared.wantLid(until: until > 0 ? Date(timeIntervalSince1970: until) : nil)
        guard until != lidRequested else { return }
        let request: [String: Any] = ["lid": until > 0, "until": until, "pid": Int(getpid())]
        guard let data = try? JSONSerialization.data(withJSONObject: request, options: [.sortedKeys]),
              (try? data.write(to: URL(fileURLWithPath: CLI.awakeRequest), options: .atomic)) != nil
        else { return }
        lidRequested = until
    }

    private func set(_ id: inout IOPMAssertionID, type: String, on: Bool) {
        if on, id == 0 {
            let reason = hotspotActive && !manualActive ? "Claude Acc: on a hotspot" : "Claude Acc: Stay Awake"
            var new: IOPMAssertionID = 0
            if IOPMAssertionCreateWithName(
                type as CFString, IOPMAssertionLevel(kIOPMAssertionLevelOn), reason as CFString, &new
            ) == kIOReturnSuccess {
                id = new
            }
        } else if !on, id != 0 {
            IOPMAssertionRelease(id)
            id = 0
        }
    }

    private func save() {
        defaults.set(autoOnHotspot, forKey: "awake.autoOnHotspot")
        defaults.set(keepDisplayOn, forKey: "awake.keepDisplayOn")
        defaults.set(keepHotspotAlive, forKey: "awake.keepHotspotAlive")
        defaults.set(lidClosed, forKey: "awake.lidClosed")
        defaults.set(manualUntil?.timeIntervalSince1970 ?? 0, forKey: "awake.until")
    }
}
