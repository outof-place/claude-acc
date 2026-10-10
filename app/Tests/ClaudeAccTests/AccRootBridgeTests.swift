import AccKit
import Foundation
import PodRootdClient
import Testing
@testable import ClaudeAcc

// Pod Menu's AccRootHelper (AccKit v0.7.0): pod-rootd's status and refusals in AccKit's terms.

@Test("the helper's status reads as AccKit's: fans, the lid lease, sysctls, the old fans daemon")
func accStatusFromHelper() {
    var status = Status()
    status.fans.mode = .fixed(FanPercent(unchecked: 60))
    status.lid.heldByUs = true
    status.lid.sleepDisabled = true
    status.lid.leaseUntil = 1_800_000_000
    status.sysctls = [
        SysctlStatus(key: .maxVnodes, current: 786_432, original: 263_168, persisted: 786_432),
        SysctlStatus(key: .gpuWiredLimitMB, current: 0, original: 0, persisted: nil),
    ]
    status.legacy = [LegacyStatus(daemon: .fans, installed: true, migrated: false)]

    #expect(
        RootHelper.accStatus(status, onAC: true)
            == AccRootStatus(
                fans: .fixed(percent: 60), lidHeldUntil: Date(timeIntervalSince1970: 1_800_000_000), lidHeld: true,
                sysctls: [.maxVnodes: 786_432, .gpuWiredLimit: 0], onAC: true, legacyFansDaemon: true))

    // a lease the helper let go of at low battery: still leased, but the Mac would sleep
    status.lid.sleepDisabled = false
    status.legacy = []
    let fresh = RootHelper.accStatus(status, onAC: false)
    #expect(fresh.lidHeldUntil != nil && !fresh.lidHeld && !fresh.legacyFansDaemon)
}

@Test("a refusal keeps the helper's sentence; a rate limit says when to try again")
func accErrorFromRefusal() {
    #expect(
        RootHelper.accError(.rateLimited(retryAfter: 1.2))
            == .refused("rate limited, retry in 2 s", retryAfter: .milliseconds(1200)))
    #expect(
        RootHelper.accError(.needsApproval(verb: "sysctl.set"))
            == .refused("sysctl.set needs your approval (Touch ID)", retryAfter: nil))
}

@Test("values outside the helper's ranges are refused before they reach the wire")
func accValuesChecked() throws {
    #expect(throws: AccRootError.refused("invalid: fan percent 10 is outside 30...100", retryAfter: nil)) {
        try PodMenuRootHelper.fanMode(.fixed(percent: 10))
    }
    #expect(try PodMenuRootHelper.fanMode(.fixed(percent: 30)) == .fixed(FanPercent(unchecked: 30)))
    #expect(throws: AccRootError.self) { try PodMenuRootHelper.sysctl(.maxVnodes(1000)) }
    #expect(try PodMenuRootHelper.sysctl(.gpuWiredLimit(megabytes: nil)) == .gpuWiredLimit(.systemDefault))
    #expect(
        try PodMenuRootHelper.sysctl(.gpuWiredLimit(megabytes: 40_960))
            == .gpuWiredLimit(.megabytes(GPUMegabytes(unchecked: 40_960))))
    #expect(PodMenuRootHelper.sysctlKey(.gpuWiredLimit) == .gpuWiredLimitMB)
}

@Test("outside Pod there is no helper: every call says so and nothing connects")
func accOutsidePod() async {
    let bridge = PodMenuRootHelper(RootHelper(appIdentifier: nil))
    let unreachable = AccRootError.unreachable(RootHelper.unreachable)
    await #expect(throws: unreachable) { try await bridge.setFans(.auto) }
    await #expect(throws: unreachable) { try await bridge.holdLid(until: .now.addingTimeInterval(3600)) }
    await #expect(throws: unreachable) { try await bridge.releaseLid() }
    await #expect(throws: unreachable) { try await bridge.status() }
}
