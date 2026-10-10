import Foundation
import LightweightCodeRequirements
import PodRootdClient
import PodRootdCore
import Security
import Testing
import XPC
@testable import ClaudeAcc

// Pod Menu and Pod's root helper (docs/pod-rootd.md): who drives the fans and the lid.

@Test("the helper takes the fans and the lid only once the old fans daemon is migrated")
func rootHelperOwnsAfterMigration() {
    var status = Status()
    status.legacy = [LegacyStatus(daemon: .fans, installed: true, migrated: false)]
    #expect(!RootHelper.owns(status))
    status.legacy = [LegacyStatus(daemon: .fans, installed: false, migrated: true)]
    #expect(RootHelper.owns(status))
    // a Mac that never had the old daemon
    status.legacy = [LegacyStatus(daemon: .fans, installed: false, migrated: false)]
    #expect(RootHelper.owns(status))
}

@Test("the standalone Claude Acc.app has no helper: nothing connects, the files stay in charge")
func rootHelperOutsidePod() {
    let helper = RootHelper(appIdentifier: nil)
    #expect(!helper.owns)
    #expect(helper.fanState == nil)
    #expect(RootHelper.hostAppIdentifier() == nil)
}

// The lid logic against the helper's real engine on the fake machine, over an anonymous XPC listener.

/// A peer policy this test process passes as Pod Menu (it is ad hoc signed: no team, no hardening).
private func admittingMe() throws -> PeerPolicy {
    var code: SecCode?
    var staticCode: SecStaticCode?
    var info: CFDictionary?
    try #require(SecCodeCopySelf([], &code) == errSecSuccess)
    try #require(SecCodeCopyStaticCode(try #require(code), [], &staticCode) == errSecSuccess)
    try #require(SecCodeCopySigningInformation(
        try #require(staticCode), SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess)
    let me = try #require((info as? [String: Any])?[kSecCodeInfoIdentifier as String] as? String)
    let requirement = XPCPeerRequirement.codeRequirement(try ProcessCodeRequirement.allOf { SigningIdentifier(me) })
    return PeerPolicy(listener: requirement, callers: [(.menu, requirement)])
}

private final class Machine {
    let backend = FakeBackend()
    let engine: Engine
    let server: Server
    var connects = 0
    var down = false

    init() throws {
        engine = Engine(backend: backend, store: MemoryStateStore())
        engine.start()
        server = Server(engine: engine, peers: try admittingMe())
    }

    /// Pod Menu's side, opening its sessions on this machine.
    func podMenu() -> RootHelper {
        RootHelper(appIdentifier: nil, connector: { [self] in
            connects += 1
            if down { throw CocoaError(.featureUnsupported) }
            return try PodRootdClient(endpoint: server.listenAnonymously())
        }, ticking: false)
    }
}

/// Until `condition` holds or a second goes by, letting the main queue run the sessions' ends.
private func eventually(_ condition: () -> Bool) async {
    for _ in 0..<100 where !condition() { try? await Task.sleep(for: .milliseconds(10)) }
}

@Test("Stay Awake's lid hold goes to the helper, and after a drop it is held again at the next tick")
func rootHelperHoldsAgainAfterDrop() async throws {
    let machine = try Machine()
    defer { machine.server.cancel() }
    let helper = machine.podMenu()
    helper.wantLid(until: .now.addingTimeInterval(3600))
    await helper.tick()
    await eventually { machine.engine.status().lid.leaseUntil != nil }
    #expect(machine.backend.sleepIsDisabled)

    // the helper restarted (or the session broke): Pod Menu reconnects at once, not after a minute
    helper.dropped()
    #expect(!helper.owns)
    await eventually { machine.engine.status().lid.leaseUntil == nil }
    #expect(machine.backend.sleepIsDisabled)  // the helper's minute keeps it meanwhile
    await helper.tick()
    await eventually { machine.engine.status().lid.leaseUntil != nil }
    #expect(helper.owns)
    #expect(machine.engine.status().lid.reholdUntil == nil)
    #expect(!machine.backend.calls.contains("setSleepDisabled false"))
}

@Test("when the old fans daemon comes back, Pod Menu lets go of its lid hold once")
func rootHelperReleasesOnLosingTheLid() async throws {
    let machine = try Machine()
    defer { machine.server.cancel() }
    let helper = machine.podMenu()
    helper.wantLid(until: .now.addingTimeInterval(3600))
    await helper.tick()
    await eventually { machine.engine.status().lid.leaseUntil != nil }
    #expect(helper.owns)

    machine.backend.legacyPlists[.fans] = ["/usr/local/libexec/claude-acc-fanctl", "daemon"]
    helper.panelOpen = true  // the next tick reads the status
    await helper.tick()
    #expect(!helper.owns)
    await eventually { machine.engine.status().lid.leaseUntil == nil }
    #expect(machine.engine.status().lid.leaseUntil == nil)
    #expect(machine.engine.status().lid.reholdUntil == nil)
    #expect(!machine.backend.sleepIsDisabled)
}

@Test("without the helper, Pod Menu tries again on a backoff: next tick, then 5 s doubling to 5 minutes")
func rootHelperBacksOff() async throws {
    #expect((1...9).map { RootHelper.backoff(after: $0) } == [0, 5, 10, 20, 40, 80, 160, 300, 300])
    let machine = try Machine()
    defer { machine.server.cancel() }
    machine.down = true
    let helper = machine.podMenu()
    await helper.tick()
    await helper.tick()
    #expect(machine.connects == 2)
    for _ in 0..<5 { await helper.tick() }
    #expect(machine.connects == 2)  // the third waits 5 s
    helper.panelOpen = true  // opening the panel tries again right away
    await helper.tick()
    #expect(machine.connects == 3)
    #expect(!helper.owns)
}

@Test("while Stay Awake wants the lid, retries never wait past 5 s, inside the helper's re-hold minute")
func rootHelperBackoffWhileHolding() {
    let now = Date(timeIntervalSince1970: 1_800_000_000)
    let holding = now.addingTimeInterval(3600)
    #expect((1...9).map { RootHelper.retryDelay(after: $0, lidWanted: holding, now: now) } == [0, 5, 5, 5, 5, 5, 5, 5, 5])
    #expect(RootHelper.retryDelay(after: 9, lidWanted: nil, now: now) == 300)
    // a hold that already ran out doesn't count
    #expect(RootHelper.retryDelay(after: 9, lidWanted: now.addingTimeInterval(-1), now: now) == 300)
}

@Test("a fan pick that isn't auto or 30-100% is refused with a sentence, not sent as 100%")
func rootHelperFanPickChecked() async throws {
    let machine = try Machine()
    defer { machine.server.cancel() }
    let helper = machine.podMenu()
    #expect(await helper.setFans("10") == "10 is not auto or 30-100%")
    #expect(await helper.setFans("loud") == "loud is not auto or 30-100%")
    #expect(machine.connects == 0)
    #expect(await helper.setFans("60") == nil)
    #expect(machine.engine.status().fans.mode == .fixed(FanPercent(unchecked: 60)))
}
