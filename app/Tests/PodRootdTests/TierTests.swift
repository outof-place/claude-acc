import Testing
@testable import PodRootdCore
import PodRootdProtocol

// The tiers (docs/pod-rootd.md, "Tiers"): A is harmless and rate-limited, open to every admitted
// caller without a prompt; B changes the system: Pod Menu on a click, pod-rootctl only with Touch ID
// or within 5 minutes of it for the same parent, Pod's Electron process never.

private let tierA: [Verb] = [
    .status, .fansSet(mode: .auto), .fansSet(mode: fan50), .lidHold(seconds: LidSeconds(600)!), .lidRelease,
    .shaperSet(interface: en0, kbps: UplinkKbps(6000)!, scope: .session),
]

private let tierB: [Verb] = [
    .powerMode(source: .ac, mode: .automatic), .sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: false),
    .sysctlReset(key: .maxVnodes), .shaperSet(interface: en0, kbps: UplinkKbps(5999)!, scope: .session),
    .shaperSet(interface: en0, kbps: UplinkKbps(20_000)!, scope: .untilReboot), .spotlightAppsOnly, .spotlightRestore,
    .fsguardSet(enabled: false, limitMB: .standard), .launchdParkOrphans(dryRun: true),
    .logsPruneDiagnostics(olderThanDays: .standard, dryRun: true), .legacyMigrate, .legacyRollback, .restoreDefaults,
]

private let shell = "sid=501 ppid=812@1791600000 tty=ttys003"

private func cli(_ rig: Rig, _ verb: Verb, authenticated: Bool = false, parent: String? = shell) -> Reply {
    let approval = parent.map { Approval(authenticated: authenticated, parent: $0) }
    return rig.engine.handle(Request(verb, approval: approval), from: .cli, session: SessionID(9))
}

@Test("the tier table: status, fans, the lid and a session's limit of 6 Mb/s or more are A, everything else is B")
func tierTable() {
    for verb in tierA { #expect(verb.tier == .a, "\(verb.summary)") }
    for verb in tierB { #expect(verb.tier == .b, "\(verb.summary)") }
    #expect(Verb.shaperClear(interface: en0).tier == .b)
}

@Test("the hotspot controller through the CLI: its own session's limit and its clearing need no Touch ID")
func sessionShaperWithoutPrompt() {
    let rig = Rig()
    rig.backend.limits["en8"] = nil
    let en8 = InterfaceName("en8")!
    #expect(cli(rig, .shaperClear(interface: en8), parent: nil).refusal == nil)  // nothing to clear yet
    #expect(cli(rig, .shaperSet(interface: en8, kbps: UplinkKbps(30_000)!, scope: .session), parent: nil).refusal == nil)
    #expect(cli(rig, .shaperSet(interface: en8, kbps: UplinkKbps(6000)!, scope: .session), parent: nil).refusal == nil)
    #expect(cli(rig, .shaperSet(interface: en8, kbps: UplinkKbps(5000)!, scope: .session), parent: nil).refusal
        == .needsApproval(verb: "shaper.set"))
    #expect(cli(rig, .shaperClear(interface: en8), parent: nil).refusal == nil)  // its own session's
    #expect(rig.backend.limits["en8"] == nil)
}

@Test("a session's limit may not cover one set until reboot, nor clear another session's, without approval")
func sessionShaperBoundaries() {
    let rig = Rig()
    rig.send(.shaperSet(interface: en0, kbps: UplinkKbps(27_000)!, scope: .untilReboot), as: .menu)
    #expect(cli(rig, .shaperSet(interface: en0, kbps: UplinkKbps(20_000)!, scope: .session), parent: nil).refusal
        == .needsApproval(verb: "shaper.set"))
    #expect(cli(rig, .shaperClear(interface: en0), parent: nil).refusal == .needsApproval(verb: "shaper.clear"))
    #expect(rig.send(.shaperClear(interface: en0), as: .app).refusal == .verbNotAllowed(verb: "shaper.clear", caller: .app))
    #expect(rig.backend.limits["en0"] == 27_000)
    // Electron Pod: a session limit of 6 Mb/s or more on a free interface, nothing below
    let en8 = InterfaceName("en8")!
    #expect(rig.send(.shaperSet(interface: en8, kbps: UplinkKbps(6000)!, scope: .session), as: .app, session: SessionID(5)).refusal == nil)
    #expect(rig.send(.shaperSet(interface: en8, kbps: UplinkKbps(1000)!, scope: .session), as: .app, session: SessionID(5)).refusal
        == .verbNotAllowed(verb: "shaper.set", caller: .app))
}

@Test("Pod's Electron process: tier A goes through but a lid hold, every tier B verb is refused before it runs")
func electronTierAOnly() {
    let rig = Rig()
    for verb in tierA where verb.name != "lid.hold" { #expect(rig.send(verb, as: .app).refusal == nil, "\(verb.name)") }
    // anything can run Electron Pod as Node, and a closed-lid hold keeps a Mac awake in a bag
    #expect(rig.send(.lidHold(seconds: LidSeconds(600)!), as: .app).refusal == .verbNotAllowed(verb: "lid.hold", caller: .app))
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.send(.lidHold(seconds: LidSeconds(600)!), as: .menu).refusal == nil)
    #expect(rig.backend.sleepIsDisabled)
    rig.send(.lidRelease, as: .menu)
    let before = rig.backend.calls
    for verb in tierB {
        #expect(rig.send(verb, as: .app).refusal == .verbNotAllowed(verb: verb.name, caller: .app), "\(verb.name)")
    }
    #expect(rig.backend.calls == before)
}

@Test("Pod Menu: tier B without a prompt, the click is the consent")
func menuTierB() {
    let rig = Rig()
    for verb in tierB { #expect(rig.send(verb, as: .menu).refusal != .needsApproval(verb: verb.name), "\(verb.name)") }
}

@Test("the CLI: tier A without a prompt; tier B refused until approved, an approval in the request lets it through")
func cliNeedsApproval() {
    let rig = Rig()
    for verb in tierA { #expect(cli(rig, verb).refusal == nil, "\(verb.name)") }
    for verb in tierB {
        #expect(cli(rig, verb).refusal == .needsApproval(verb: verb.name), "\(verb.name)")
        #expect(cli(rig, verb, parent: nil).refusal == .needsApproval(verb: verb.name), "\(verb.name)")
    }
    // only tier A reached the machine
    #expect(rig.backend.calls.allSatisfy { ["applyFans", "setSleepDisabled", "setUplinkLimit en0 6000"].contains(where: $0.hasPrefix) })
    #expect(cli(rig, .spotlightAppsOnly, authenticated: true).refusal == nil)
}

@Test("the grace: 5 minutes for the same parent after a Touch ID, not for another parent, not after")
func cliGrace() {
    let rig = Rig()
    #expect(cli(rig, .spotlightAppsOnly, authenticated: true).refusal == nil)
    rig.clock.advance(299)
    #expect(cli(rig, .spotlightRestore).refusal == nil)
    #expect(cli(rig, .spotlightRestore, parent: "sid=501 ppid=999@1791600500 tty=ttys003").refusal == .needsApproval(verb: "spotlight.restore"))
    // the grace runs from the Touch ID, not from the last use
    rig.clock.advance(2)
    #expect(cli(rig, .spotlightRestore).refusal == .needsApproval(verb: "spotlight.restore"))
    #expect(cli(rig, .spotlightRestore, authenticated: true).refusal == nil)
}

@Test("an approval from Pod Menu or Electron changes nothing: Electron stays off tier B")
func approvalIgnoredElsewhere() {
    let rig = Rig()
    let approval = Approval(authenticated: true, parent: shell)
    #expect(rig.engine.handle(Request(.restoreDefaults, approval: approval), from: .app, session: SessionID(1)).refusal
        == .verbNotAllowed(verb: "restoreDefaults", caller: .app))
}
