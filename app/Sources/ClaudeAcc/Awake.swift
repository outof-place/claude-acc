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
    /// macOS marks a path "expensive" for every hotspot: an iPhone over Wi-Fi or USB,
    /// an Android with the metered hint, any network in Low Data Mode.
    private(set) var onHotspot = false
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

    private static let probe = URL(string: "http://captive.apple.com/hotspot-detect.html")!
    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 6
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    init(preview: Bool = false) {
        autoOnHotspot = defaults.object(forKey: "awake.autoOnHotspot") as? Bool ?? true
        keepDisplayOn = defaults.bool(forKey: "awake.keepDisplayOn")
        keepHotspotAlive = defaults.object(forKey: "awake.keepHotspotAlive") as? Bool ?? true
        lidClosed = defaults.object(forKey: "awake.lidClosed") as? Bool ?? true
        self.preview = preview
        let until = defaults.double(forKey: "awake.until")
        manualUntil = until > 0 ? Date(timeIntervalSince1970: until) : nil
        guard !preview else { return }
        monitor.pathUpdateHandler = { [weak self] path in
            let hotspot = path.status == .satisfied && path.isExpensive
            let via: String? = if path.usesInterfaceType(.wifi) {
                "Wi-Fi"
            } else if path.usesInterfaceType(.wiredEthernet) {
                "USB"
            } else if path.usesInterfaceType(.cellular) {
                "cellular"
            } else {
                nil
            }
            Task { @MainActor in self?.pathChanged(hotspot: hotspot, via: via) }
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

    // MARK: Internals

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
    }

    /// An assertion stops idle sleep only: a closed lid sleeps a MacBook on battery anyway. The fan
    /// daemon (root) holds `SleepDisabled` while this file asks for it, and only as long as this
    /// pid runs. A session that started itself on a hotspot doesn't ask: a Mac awake in a bag has
    /// to be a choice. Written when the request changes, not on every tick.
    private func requestLid() {
        guard !preview else { return }
        let until = manualActive && lidClosed ? manualUntil?.timeIntervalSince1970 ?? 0 : 0
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
