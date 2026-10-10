import AccKit
import Foundation
import IOKit.ps
import PodRootdClient

/// Pod's root helper as AccKit's views see it: Pod Menu hands this to AccStore. It keeps nothing of
/// its own; every call goes to RootHelper on the main actor, so the views and Awake share one XPC
/// session and one lid hold.
nonisolated struct PodMenuRootHelper: AccRootHelper {
    let helper: RootHelper

    @MainActor init(_ helper: RootHelper = .shared) {
        self.helper = helper
    }

    func setFans(_ mode: AccFanMode) async throws(AccRootError) {
        try await helper.send(.fansSet(mode: Self.fanMode(mode)))
    }

    func holdLid(until: Date) async throws(AccRootError) {
        try await helper.holdLid(until: until)
    }

    func releaseLid() async throws(AccRootError) {
        try await helper.holdLid(until: nil)
    }

    func setSysctl(_ setting: AccSysctl, persist: Bool) async throws(AccRootError) {
        try await helper.send(.sysctlSet(setting: Self.sysctl(setting), persist: persist))
    }

    func resetSysctl(_ key: AccSysctl.Key) async throws(AccRootError) {
        try await helper.send(.sysctlReset(key: Self.sysctlKey(key)))
    }

    func status() async throws(AccRootError) -> AccRootStatus {
        try await helper.send(.status)
        return await helper.accStatus
    }

    // MARK: AccKit's values as verbs

    /// A value outside the helper's range is refused here in the helper's own words, so it never
    /// goes on the wire unchecked.
    static func fanMode(_ mode: AccFanMode) throws(AccRootError) -> FanMode {
        switch mode {
        case .auto: return .auto
        case .fixed(let percent):
            guard let value = FanPercent(percent) else { throw invalid("fan percent \(percent)", FanPercent.allowed) }
            return .fixed(value)
        }
    }

    static func sysctl(_ setting: AccSysctl) throws(AccRootError) -> SysctlSetting {
        switch setting {
        case .maxVnodes(let count):
            guard let value = MaxVnodes(count) else { throw invalid("kern.maxvnodes \(count)", MaxVnodes.allowed) }
            return .maxVnodes(value)
        case .gpuWiredLimit(megabytes: nil):
            return .gpuWiredLimit(.systemDefault)
        case .gpuWiredLimit(megabytes: let megabytes?):
            guard let value = GPUMegabytes(megabytes) else {
                throw invalid("iogpu.wired_limit_mb \(megabytes)", GPUMegabytes.allowed)
            }
            return .gpuWiredLimit(.megabytes(value))
        }
    }

    static func sysctlKey(_ key: AccSysctl.Key) -> SysctlKey {
        switch key {
        case .maxVnodes: .maxVnodes
        case .gpuWiredLimit: .gpuWiredLimitMB
        }
    }

    private static func invalid(_ what: String, _ allowed: ClosedRange<Int64>) -> AccRootError {
        .refused("\(Refusal.invalid("\(what) is outside \(allowed)"))", retryAfter: nil)
    }
}

extension RootHelper {
    static let unreachable = "Pod's root helper doesn't answer"

    /// The helper's status in AccKit's terms; the power source is read here, the helper doesn't
    /// report it.
    var accStatus: AccRootStatus {
        status.map { Self.accStatus($0, onAC: Self.onAC()) } ?? AccRootStatus(onAC: Self.onAC())
    }

    static func accStatus(_ status: Status, onAC: Bool?) -> AccRootStatus {
        var sysctls: [AccSysctl.Key: Int] = [:]
        for entry in status.sysctls {
            guard let current = entry.current else { continue }
            switch entry.key {
            case .maxVnodes: sysctls[.maxVnodes] = Int(current)
            case .gpuWiredLimitMB: sysctls[.gpuWiredLimit] = Int(current)
            }
        }
        let fans: AccFanMode =
            switch status.fans.mode {
            case .auto: .auto
            case .fixed(let percent): .fixed(percent: Int(percent.value))
            }
        return AccRootStatus(
            fans: fans, lidHeldUntil: status.lid.leaseUntil.map(Date.init(timeIntervalSince1970:)),
            lidHeld: status.lid.sleepDisabled, sysctls: sysctls, onAC: onAC, legacyFansDaemon: !owns(status))
    }

    /// Every refusal keeps the helper's sentence; a rate limit also says when to try again.
    static func accError(_ refusal: Refusal) -> AccRootError {
        if case .rateLimited(let after) = refusal {
            return .refused(refusal.description, retryAfter: .milliseconds(Int64((after * 1000).rounded(.up))))
        }
        return .refused(refusal.description, retryAfter: nil)
    }

    /// On the power adapter; nil when IOKit has no power source information.
    static func onAC() -> Bool? {
        guard let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let source = IOPSGetProvidingPowerSourceType(blob)?.takeUnretainedValue() as String?
        else { return nil }
        return source == kIOPSACPowerValue
    }
}
