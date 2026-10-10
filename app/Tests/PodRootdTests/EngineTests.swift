import Foundation
import Testing
@testable import PodRootdCore
import PodRootdProtocol

// The engine on the fake machine: each verb idempotent, the rules fanctl, Lid and fsguard.py had,
// what survives a restart, the migration and the restore.

private let hour = LidSeconds(3600)!

// MARK: Fans

@Test("a fixed fan setting goes to the SMC once; the same setting again changes nothing")
func fansIdempotent() {
    let rig = Rig()
    #expect(rig.changed(rig.send(.fansSet(mode: fan50))) == true)
    #expect(rig.changed(rig.send(.fansSet(mode: fan50))) == false)
    rig.engine.tick()
    #expect(rig.backend.calls == ["applyFans 50%"])
    #expect(rig.changed(rig.send(.fansSet(mode: .auto))) == true)
    #expect(rig.changed(rig.send(.fansSet(mode: .auto))) == false)
    #expect(rig.backend.calls == ["applyFans 50%", "applyFans auto"])
}

@Test("95 °C under a fixed setting gives full speed, below 85 °C the setting comes back")
func fansBoost() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.backend.hottest = 96
    rig.engine.tick()
    #expect(rig.engine.status().fans.boosting)
    #expect(rig.engine.status().fans.applied == .fixed(FanPercent(100)!))
    rig.backend.hottest = 90
    rig.engine.tick()
    #expect(rig.engine.status().fans.applied == .fixed(FanPercent(100)!))
    rig.backend.hottest = 80
    rig.engine.tick()
    #expect(!rig.engine.status().fans.boosting)
    #expect(rig.backend.calls == ["applyFans 50%", "applyFans 100%", "applyFans 50%"])
}

@Test("a setting the SMC lost over sleep comes back; another app's setting is reported, not fought")
func fansSleepAndConflict() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    for i in rig.backend.fans.indices { rig.backend.fans[i].manual = false }
    rig.engine.tick()
    #expect(rig.backend.calls == ["applyFans 50%", "applyFans 50%"])
    rig.backend.fans[0].target = rig.backend.fans[0].max
    rig.engine.tick()
    rig.engine.tick()
    #expect(rig.engine.status().fans.conflict)
    #expect(rig.backend.calls.count == 2)
    // picking a mode again takes the fans back
    rig.send(.fansSet(mode: fan50))
    #expect(!rig.engine.status().fans.conflict)
    #expect(rig.backend.calls.count == 3)
}

@Test("an SMC write that fails hands the fans back to macOS and says why")
func fansFailure() {
    let rig = Rig()
    rig.backend.failing = ["applyFans"]
    let reply = rig.send(.fansSet(mode: fan50))
    guard case .failed? = reply.refusal else {
        Issue.record("expected a failure, got \(reply.outcome)")
        return
    }
    #expect(reply.status?.fans.error != nil)
}

// MARK: Lid

@Test("a lid hold turns SleepDisabled on; a release turns it off at once, a session's end after a minute")
func lidLease() {
    let rig = Rig()
    #expect(rig.changed(rig.send(.lidHold(seconds: hour))) == true)
    #expect(rig.backend.sleepIsDisabled)
    #expect(rig.changed(rig.send(.lidHold(seconds: hour))) == false)
    rig.send(.lidRelease)
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "released")
    rig.send(.lidHold(seconds: hour), session: SessionID(7))
    rig.engine.sessionEnded(SessionID(7))
    // a dropped session: kept for Pod Menu to hold it again
    #expect(rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.reholdUntil == rig.clock.now + Engine.lidRehold)
    rig.clock.advance(Engine.lidRehold + 1)
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "session ended")
    #expect(rig.engine.status().lid.reholdUntil == nil)
}

@Test("a dropped session held again within the minute: SleepDisabled never goes off")
func lidReheld() {
    let rig = Rig()
    rig.send(.lidHold(seconds: hour), session: SessionID(7))
    rig.engine.sessionEnded(SessionID(7))
    rig.clock.advance(5)
    rig.engine.tick()
    rig.send(.lidHold(seconds: hour), session: SessionID(8))
    #expect(rig.engine.status().lid.reholdUntil == nil)
    rig.clock.advance(Engine.lidRehold + 1)
    rig.engine.tick()
    #expect(rig.backend.sleepIsDisabled)
    #expect(!rig.backend.calls.contains("setSleepDisabled false"))
    // the window keeps a hold, it never takes one: low battery still ends it
    rig.engine.sessionEnded(SessionID(8))
    rig.backend.lowBattery = true
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "battery")
}

@Test("two sessions holding the lid: it stays held until the last one goes")
func lidTwoSessions() {
    let rig = Rig()
    rig.send(.lidHold(seconds: hour), session: SessionID(1))
    rig.send(.lidHold(seconds: hour), session: SessionID(2))
    rig.engine.sessionEnded(SessionID(1))
    #expect(rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.reholdUntil == nil)
    rig.engine.sessionEnded(SessionID(2))
    rig.clock.advance(Engine.lidRehold + 1)
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
}

@Test("the hold ends on time, at 10% battery, and when the Mac gets hot (then not again for 15 minutes)")
func lidLetsGo() {
    let rig = Rig()
    rig.send(.lidHold(seconds: LidSeconds(60)!))
    rig.clock.advance(61)
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "expired")

    rig.send(.lidHold(seconds: hour))
    rig.backend.lowBattery = true
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "battery")
    rig.backend.lowBattery = false
    rig.engine.tick()
    #expect(rig.backend.sleepIsDisabled)

    rig.backend.hot = true
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "thermal")
    rig.backend.hot = false
    rig.clock.advance(14 * 60)
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    rig.clock.advance(2 * 60)
    rig.engine.tick()
    #expect(rig.backend.sleepIsDisabled)
}

@Test("someone else's SleepDisabled (Amphetamine, pmset by hand) is never turned off")
func lidLeavesOthersAlone() {
    let rig = Rig()
    rig.backend.sleepIsDisabled = true
    rig.send(.lidHold(seconds: hour))
    #expect(!rig.engine.status().lid.heldByUs)
    rig.send(.lidRelease)
    #expect(rig.backend.sleepIsDisabled)
    #expect(!rig.backend.calls.contains { $0.hasPrefix("setSleepDisabled") })
}

// MARK: Sysctls

@Test("sysctl set keeps the value from before; reset puts it back; the same value twice changes nothing")
func sysctlSetReset() {
    let rig = Rig()
    let vnodes = SysctlSetting.maxVnodes(MaxVnodes(786_432)!)
    #expect(rig.changed(rig.send(.sysctlSet(setting: vnodes, persist: false))) == true)
    #expect(rig.changed(rig.send(.sysctlSet(setting: vnodes, persist: false))) == false)
    let status = rig.engine.status().sysctls.first { $0.key == .maxVnodes }
    #expect(status?.current == 786_432)
    #expect(status?.original == 263_168)
    #expect(status?.persisted == nil)
    #expect(rig.changed(rig.send(.sysctlReset(key: .maxVnodes))) == true)
    #expect(rig.backend.sysctls[.maxVnodes] == 263_168)
    #expect(rig.changed(rig.send(.sysctlReset(key: .maxVnodes))) == false)
}

@Test("a persisted sysctl comes back after a reboot, and the new boot's default becomes the original")
func sysctlPersistAcrossBoot() {
    let rig = Rig()
    rig.send(.sysctlSet(setting: .gpuWiredLimit(.megabytes(GPUMegabytes(40_960)!)), persist: true))
    rig.send(.sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: false))
    // reboot: the kernel forgot both
    rig.backend.bootTime += 86_400
    rig.backend.sysctls = [.maxVnodes: 300_000, .gpuWiredLimitMB: 0]
    let after = rig.restarted()
    #expect(rig.backend.sysctls[.gpuWiredLimitMB] == 40_960)
    #expect(rig.backend.sysctls[.maxVnodes] == 300_000)
    let gpu = after.engine.status().sysctls.first { $0.key == .gpuWiredLimitMB }
    #expect(gpu?.persisted == 40_960)
    #expect(gpu?.original == 0)
    #expect(after.engine.state.sysctls[SysctlKey.maxVnodes.rawValue] == nil)
}

@Test("a GPU limit that leaves the system under 4 GB is refused; a value the kernel won't take is undone")
func sysctlRefusals() {
    let rig = Rig()
    let tooMuch = rig.send(.sysctlSet(setting: .gpuWiredLimit(.megabytes(GPUMegabytes(46_000)!)), persist: false))
    guard case .invalid? = tooMuch.refusal else {
        Issue.record("expected invalid, got \(tooMuch.outcome)")
        return
    }
    rig.backend.stubbornSysctls = [.maxVnodes]
    let stubborn = rig.send(.sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: true))
    guard case .failed? = stubborn.refusal else {
        Issue.record("expected failed, got \(stubborn.outcome)")
        return
    }
    #expect(rig.engine.state.sysctls.isEmpty)
}

// MARK: Upload limit

@Test("shaper set keeps the limit from before; clear puts it back; rounding by ifconfig is the same rate")
func shaperSetClear() {
    let rig = Rig()
    rig.backend.limits["en0"] = 650_000
    #expect(rig.changed(rig.send(.shaperSet(interface: en0, kbps: UplinkKbps(27_000)!, scope: .untilReboot))) == true)
    rig.backend.limits["en0"] = 27_004  // ifconfig prints 27.00 Mbps
    #expect(rig.changed(rig.send(.shaperSet(interface: en0, kbps: UplinkKbps(27_000)!, scope: .untilReboot))) == false)
    rig.send(.shaperClear(interface: en0))
    #expect(rig.backend.limits["en0"] == 650_000)
    #expect(rig.engine.state.shapers.isEmpty)
}

@Test("a session's limit (the hotspot controller) goes when its session ends; a missing interface is refused")
func shaperSession() {
    let rig = Rig()
    rig.send(.shaperSet(interface: InterfaceName("en8")!, kbps: UplinkKbps(30_000)!, scope: .session), session: SessionID(4))
    rig.send(.shaperSet(interface: InterfaceName("en8")!, kbps: UplinkKbps(27_000)!, scope: .session), session: SessionID(4))
    #expect(rig.backend.limits["en8"] == 27_000)
    #expect(!rig.engine.isIdle)
    rig.engine.sessionEnded(SessionID(4))
    #expect(rig.backend.limits["en8"] == nil)
    #expect(rig.engine.isIdle)
    let missing = rig.send(.shaperSet(interface: InterfaceName("en5")!, kbps: UplinkKbps(1000)!, scope: .session))
    #expect(missing.refusal == .unsupported("no interface en5"))
}

// MARK: Spotlight and power

@Test("apps-only saves the Privacy list once; restore puts it back; both twice change nothing")
func spotlight() {
    let rig = Rig()
    let before = rig.backend.exclusions
    #expect(rig.changed(rig.send(.spotlightAppsOnly)) == true)
    #expect(rig.changed(rig.send(.spotlightAppsOnly)) == false)
    #expect(rig.backend.exclusions == rig.backend.appsOnly)
    #expect(rig.backend.spotlightReloads == 1)
    #expect(rig.engine.status().spotlight.savedEntries == before.count)
    #expect(rig.changed(rig.send(.spotlightRestore)) == true)
    #expect(rig.backend.exclusions == before)
    #expect(rig.changed(rig.send(.spotlightRestore)) == false)
}

@Test("high power mode only where the Mac has it; the first change remembers the mode before")
func powerMode() {
    let rig = Rig()
    rig.backend.highPowerCapable = false
    #expect(rig.send(.powerMode(source: .ac, mode: .high)).refusal == .unsupported("this Mac has no high power mode"))
    rig.backend.highPowerCapable = true
    #expect(rig.changed(rig.send(.powerMode(source: .ac, mode: .high))) == true)
    #expect(rig.changed(rig.send(.powerMode(source: .ac, mode: .high))) == false)
    #expect(rig.engine.status().power.originalAC == .automatic)
}

// MARK: Rate limits

@Test("fans: three changes at once, the fourth waits; a second later one more goes through")
func rateLimit() {
    let rig = Rig(limits: RateLimiter.standard)
    for percent in [40, 50, 60] {
        #expect(rig.send(.fansSet(mode: .fixed(FanPercent(percent)!))).refusal == nil)
    }
    guard case .rateLimited(let after)? = rig.send(.fansSet(mode: .fixed(FanPercent(70)!))).refusal else {
        Issue.record("expected a rate limit")
        return
    }
    #expect(after > 0 && after <= 2)
    // another caller has its own bucket, and status is its own class
    #expect(rig.send(.fansSet(mode: .fixed(FanPercent(70)!)), as: .cli).refusal == nil)
    #expect(rig.send(.status).refusal == nil)
    rig.clock.advance(2)
    #expect(rig.send(.fansSet(mode: .fixed(FanPercent(70)!))).refusal == nil)
}

// MARK: fsguard

@Test("fsguard restarts fseventsd after two readings over the limit, then waits 5 minutes")
func fsguard() {
    let rig = Rig()
    rig.send(.fsguardSet(enabled: true, limitMB: .standard))
    rig.engine.tick()
    #expect(rig.engine.status().fsguard.footprintMB == 40)
    rig.backend.footprints["fseventsd"]?.bytes = 5 << 30
    rig.clock.advance(60)
    rig.engine.tick()
    #expect(rig.backend.terminated.isEmpty)
    rig.clock.advance(60)
    rig.engine.tick()
    #expect(rig.backend.terminated == [321])
    #expect(rig.engine.status().fsguard.generation == 1)
    // still over: two more readings, but within 5 minutes of the restart
    rig.clock.advance(60)
    rig.engine.tick()
    rig.clock.advance(60)
    rig.engine.tick()
    #expect(rig.backend.terminated.count == 1)
    rig.clock.advance(240)
    rig.engine.tick()
    #expect(rig.backend.terminated.count == 2)
    // between minutes a tick reads nothing
    rig.clock.advance(10)
    rig.engine.tick()
    #expect(rig.backend.terminated.count == 2)
}

// MARK: Restart and shutdown

@Test("after a restart a fixed fan setting is applied again; a lid hold of ours waits a minute for Pod Menu")
func restart() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.send(.lidHold(seconds: hour))
    // the helper dies; the SMC forgets the fans
    for i in rig.backend.fans.indices { rig.backend.fans[i].manual = false }
    let after = rig.restarted()
    #expect(after.engine.status().fans.applied == fan50)
    #expect(rig.backend.sleepIsDisabled)
    #expect(!after.engine.isIdle)
    rig.clock.advance(Engine.lidRehold + 1)
    after.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(after.engine.status().lid.lastRelease == "helper restarted")

    // held again in time, by the session Pod Menu opens after the restart
    after.send(.lidHold(seconds: hour))
    let again = after.restarted()
    again.send(.lidHold(seconds: hour), session: SessionID(2))
    rig.clock.advance(Engine.lidRehold + 1)
    again.engine.tick()
    #expect(rig.backend.sleepIsDisabled)
}

@Test("SIGTERM hands the fans back and lets go of the lid; the fan mode stays for the next start")
func shutdown() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.send(.lidHold(seconds: hour))
    rig.send(.shaperSet(interface: en0, kbps: UplinkKbps(20_000)!, scope: .session))
    rig.engine.shutdown()
    #expect(rig.backend.fans.allSatisfy { !$0.manual })
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.backend.limits["en0"] == nil)
    #expect(rig.store.saved?.fans == fan50)
    #expect(rig.store.saved?.lidHeldByUs == false)
}

@Test("idle: nothing held, so the helper may exit and wait for launchd")
func idle() {
    let rig = Rig()
    #expect(rig.engine.isIdle)
    #expect(rig.engine.nextTickDelay == nil)
    rig.send(.fansSet(mode: fan50))
    #expect(!rig.engine.isIdle)
    #expect(rig.engine.nextTickDelay == 2)
    rig.send(.fansSet(mode: .auto))
    rig.send(.fsguardSet(enabled: true, limitMB: .standard))
    #expect(!rig.engine.isIdle)
}

// MARK: The five old daemons

private func legacyMachine() -> FakeBackend {
    let backend = FakeBackend()
    let config = "/Users/x/.local/share/claude-acc/fans.json"
    backend.legacyPlists = [
        .fans: ["/usr/local/libexec/claude-acc-fanctl", "daemon", "--config", config, "--state", "/Users/x/s.json"],
        .vnodes: ["/usr/sbin/sysctl", "-w", "kern.maxvnodes=786432"],
        .iogpu: ["/usr/sbin/sysctl", "-w", "iogpu.wired_limit_mb=40960"],
        .fsguard: ["/usr/local/libexec/claude-acc-fsguard"],
        .hotspot: ["/usr/local/libexec/claude-acc-hotspot", "daemon"],
    ]
    backend.legacyBooted = Set(LegacyDaemon.allCases)
    backend.fanConfigs = [config: .fixed(FanPercent(60)!)]
    backend.sysctls = [.maxVnodes: 786_432, .gpuWiredLimitMB: 40_960]
    backend.savedSpotlightList = ["/Users/x/Movies"]
    return backend
}

@Test("migrate takes over what the five old daemons did, moves their plists aside, and is a no-op the second time")
func legacyMigrate() {
    let rig = Rig(backend: legacyMachine())
    let reply = rig.send(.legacyMigrate)
    guard case .done(true, _, .legacy(let daemons)?) = reply.outcome else {
        Issue.record("unexpected \(reply.outcome)")
        return
    }
    #expect(Set(daemons) == Set(LegacyDaemon.allCases))
    #expect(rig.backend.legacyPlists.isEmpty)
    #expect(rig.backend.legacyAside.count == 5)
    #expect(rig.backend.calls.filter { $0.hasPrefix("bootout") }.count == 5)
    let status = rig.engine.status()
    #expect(status.fans.mode == .fixed(FanPercent(60)!))
    #expect(status.sysctls.first { $0.key == .maxVnodes }?.persisted == 786_432)
    #expect(status.sysctls.first { $0.key == .gpuWiredLimitMB }?.persisted == 40_960)
    // the old daemon's limit is not the original: undoing it after migration goes back to macOS's 0 now
    #expect(status.sysctls.first { $0.key == .gpuWiredLimitMB }?.original == 0)
    #expect(status.fsguard.enabled)
    #expect(status.spotlight.appsOnly)
    // perf-root.sh's saved list went aside: after a restore apps-only stays the helper's
    #expect(rig.backend.savedSpotlightList == nil)
    #expect(rig.backend.savedSpotlightAside == ["/Users/x/Movies"])
    #expect(status.legacy.allSatisfy { $0.migrated && !$0.installed })
    #expect(rig.changed(rig.send(.legacyMigrate)) == false)
    // iogpu undo after the migration: macOS's default now, not at the next boot
    rig.send(.sysctlReset(key: .gpuWiredLimitMB))
    #expect(rig.backend.sysctls[.gpuWiredLimitMB] == 0)
}

@Test("rollback puts the plists back, starts them, and turns the helper's copies of their work off")
func legacyRollback() {
    let rig = Rig(backend: legacyMachine())
    rig.send(.legacyMigrate)
    #expect(rig.changed(rig.send(.legacyRollback)) == true)
    #expect(rig.backend.legacyPlists.count == 5)
    #expect(rig.backend.legacyAside.isEmpty)
    #expect(rig.backend.calls.filter { $0.hasPrefix("bootstrap") }.count == 5)
    let status = rig.engine.status()
    #expect(status.fans.mode == .auto)
    #expect(status.sysctls.allSatisfy { $0.persisted == nil })
    #expect(!status.fsguard.enabled)
    #expect(status.legacy.allSatisfy { $0.installed && !$0.migrated })
    // the root copy's list is back where perf-root.sh keeps it, and the helper lets go of its copy
    #expect(rig.backend.savedSpotlightList == ["/Users/x/Movies"])
    #expect(rig.backend.savedSpotlightAside == nil)
    #expect(status.spotlight.savedEntries == nil && !status.spotlight.appsOnly)
    #expect(rig.changed(rig.send(.legacyRollback)) == false)
}

@Test("migrate without the old daemons changes nothing")
func legacyNothing() {
    let rig = Rig()
    #expect(rig.changed(rig.send(.legacyMigrate)) == false)
    #expect(rig.backend.calls.isEmpty)
}

// MARK: Restore

@Test("restoreDefaults: fans auto, sleep allowed, sysctls and limits and Spotlight and power back, nothing persisted")
func restoreDefaults() {
    let rig = Rig()
    rig.backend.limits["en0"] = 650_000
    rig.send(.fansSet(mode: fan50))
    rig.send(.lidHold(seconds: hour))
    rig.send(.sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: true))
    rig.send(.sysctlSet(setting: .gpuWiredLimit(.megabytes(GPUMegabytes(40_960)!)), persist: true))
    rig.send(.shaperSet(interface: en0, kbps: UplinkKbps(27_000)!, scope: .untilReboot))
    rig.send(.spotlightAppsOnly)
    rig.send(.powerMode(source: .ac, mode: .high))
    rig.send(.fsguardSet(enabled: true, limitMB: .standard))
    let before = rig.backend.calls.count
    #expect(rig.changed(rig.send(.restoreDefaults)) == true)
    #expect(rig.backend.fans.allSatisfy { !$0.manual })
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.backend.sysctls == [.maxVnodes: 263_168, .gpuWiredLimitMB: 0])
    #expect(rig.backend.limits["en0"] == 650_000)
    #expect(rig.backend.exclusions == ["/Users/x/Movies"])
    #expect(rig.backend.powerModes[.ac] == .automatic)
    let state = rig.store.saved
    #expect(state?.fans == .auto)
    #expect(state?.sysctls.isEmpty == true)
    #expect(state?.shapers.isEmpty == true)
    #expect(state?.spotlightApplied == false)
    #expect(state?.powerOriginal.isEmpty == true)
    #expect(state?.fsguard.enabled == false)
    #expect(rig.engine.isIdle)
    // and again: nothing left to do
    let after = rig.backend.calls.count
    #expect(after > before)
    #expect(rig.changed(rig.send(.restoreDefaults)) == false)
    #expect(rig.backend.calls.count == after)
}

@Test("a restore that partly fails reports what failed and still does the rest")
func restorePartialFailure() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.send(.sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: true))
    rig.backend.failing = ["setSysctl"]
    let reply = rig.send(.restoreDefaults)
    guard case .failed(let why)? = reply.refusal else {
        Issue.record("expected a failure, got \(reply.outcome)")
        return
    }
    #expect(why.contains("kern.maxvnodes"))
    #expect(rig.backend.fans.allSatisfy { !$0.manual })
}

// MARK: System verbs

@Test("orphan launchd plists: a dry run only lists them, a real run parks them")
func orphans() {
    let rig = Rig()
    let orphan = Orphan(label: "com.gone.helper", plist: "/Library/LaunchDaemons/com.gone.helper.plist",
                        program: "/Library/PrivilegedHelperTools/com.gone.helper", domain: "system")
    rig.backend.orphans = [orphan]
    #expect(rig.send(.launchdParkOrphans(dryRun: true)).outcome == .done(changed: false, note: nil, report: .orphans([orphan], parked: false)))
    #expect(rig.backend.parked.isEmpty)
    #expect(rig.send(.launchdParkOrphans(dryRun: false)).outcome == .done(changed: true, note: nil, report: .orphans([orphan], parked: true)))
    #expect(rig.backend.parked == [orphan])
    #expect(rig.changed(rig.send(.launchdParkOrphans(dryRun: false))) == false)
}

@Test("every change goes to the state file; a status read doesn't write it")
func persistence() {
    let rig = Rig()
    let saves = rig.store.saves
    rig.send(.status)
    rig.send(.status)
    #expect(rig.store.saves == saves)
    rig.send(.fansSet(mode: fan50))
    #expect(rig.store.saves == saves + 1)
    #expect(rig.store.saved?.fans == fan50)
}

// MARK: The package's install

private let podApp = "/Applications/Pod.app/Contents/Resources/claude-acc/pod-rootd"
private let openedFrom = "/Users/x/Downloads/Pod.app/Contents/Resources/claude-acc/pod-rootd"

@Test("a session from Pod or Pod Menu looks for a newer helper in Pod.app at most once an hour; the CLI's never")
func updateLooksHourly() async {
    let rig = Rig(updates: true)
    rig.backend.updates.candidates = [podApp: .init(version: "57")]
    rig.send(.status, as: .cli)
    #expect(rig.engine.nextTickDelay == nil)
    rig.send(.status, as: .menu)
    #expect(rig.engine.nextTickDelay == 0)
    #expect(!rig.engine.isIdle)
    var restarted = false
    rig.engine.onUpdateInstalled = { restarted = true }
    rig.engine.tick()
    #expect(rig.engine.updating)  // the look runs on its own queue
    #expect(!rig.engine.isIdle)
    await settled { !rig.engine.updating }
    #expect(rig.engine.restartForUpdate && restarted)
    #expect(rig.backend.installedVersion == "57")

    let after = rig.restarted()
    after.backend.updates.candidates = [podApp: .init(version: "58")]
    after.send(.status, as: .app)
    after.engine.tick()
    await settled { !after.engine.updating }
    #expect(after.backend.installedVersion == "58")
    let again = after.restarted()
    again.send(.status, as: .app)
    again.engine.tick()
    await settled { !again.engine.updating }
    again.send(.status, as: .app)
    #expect(again.engine.nextTickDelay == nil)  // the hour hasn't passed for this process
}

@Test("only a genuine, higher version replaces the helper; the check reads the staged copy, never the candidate")
func updateTakesOnlyNewerGenuine() async {
    let rig = Rig(updates: true)
    rig.backend.updates.candidates = [
        openedFrom: .init(version: "56"),  // the same build
        podApp: .init(version: "60", genuine: false),  // a re-signed or ad hoc copy
    ]
    rig.send(.status, as: .menu)
    rig.engine.tick()
    await settled { !rig.engine.updating }
    #expect(!rig.engine.restartForUpdate)
    #expect(rig.backend.installedVersion == "56")
    #expect(rig.backend.updates.staged.isEmpty)
    let checks = rig.backend.updates.calls.filter { $0.hasPrefix("verify") }
    #expect(checks.allSatisfy { $0.hasPrefix("verify /staged/") })
    #expect(checks.count == 2)
    #expect(!rig.backend.calls.contains { $0.hasPrefix("installUpdate") })
}

@Test("an older genuine build, a symlinked candidate or a version that isn't a number never goes in")
func updateRefusesDowngradesAndLinks() async {
    let rig = Rig(updates: true)
    rig.backend.updates.candidates = [
        openedFrom: .init(version: "55"),
        podApp: .init(version: "99", symlink: true),
    ]
    rig.send(.status, as: .menu)
    rig.engine.tick()
    await settled { !rig.engine.updating }
    #expect(rig.backend.installedVersion == "56")
    #expect(!rig.backend.updates.calls.contains("verify /staged/1"))
    #expect(Engine.newer("57", than: "56"))
    #expect(Engine.newer("1.31.10", than: "1.31.9"))
    #expect(!Engine.newer("56", than: "56"))
    #expect(!Engine.newer("55", than: "56"))
    #expect(!Engine.newer("57-beta", than: "56"))
    #expect(!Engine.newer("", than: "56"))
}

/// The lid held, then the Mac gets hot while a look is still out: the main queue must let go now.
private func heatWhileLooking(_ rig: Rig) {
    #expect(rig.engine.updating)
    #expect(rig.backend.sleepIsDisabled)
    rig.backend.hot = true
    rig.engine.tick()
    #expect(!rig.backend.sleepIsDisabled)
    #expect(rig.engine.status().lid.lastRelease == "thermal")
}

@Test("a look that stalls (a slow mount) is given up at the deadline; the lid and heat rules run meanwhile")
func updateStallHitsTheDeadline() async {
    let rig = Rig(updates: true, updateRunner: UpdateRunner(deadline: 0.3))
    rig.backend.updates.candidates = [podApp: .init(version: "57")]
    rig.backend.updates.stall = 1.5
    rig.send(.lidHold(seconds: hour))
    rig.engine.tick()
    heatWhileLooking(rig)
    await settled { !rig.engine.updating }
    #expect(!rig.engine.updating)
    #expect(!rig.engine.restartForUpdate)
    // the late answer's copy is thrown away: a timeout means skip
    await settled { rig.backend.updates.calls.contains { $0.hasPrefix("discard") } }
    #expect(rig.backend.updates.staged.isEmpty)
    #expect(rig.backend.installedVersion == "56")
}

/// Real files: HelperFiles.stage into a scratch PrivilegedHelperTools; any copy counts as version 99.
nonisolated private struct ScratchSource: UpdateSource {
    let directory: String
    var local: Bool = true

    func stage(_ candidate: String) throws -> String {
        try HelperFiles.stage(candidate, into: directory, name: "codes.pod.app.rootd", onLocalVolume: { _ in local })
    }

    func verifiedVersion(ofStaged path: String) -> String? { "99" }

    func discard(_ staged: String) { unlink(staged) }
}

@Test("a FIFO in place of the candidate can't block the look, and the lid and heat rules run meanwhile",
      .timeLimit(.minutes(1)))
func updateFifoCandidate() async throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pod-rootd-fifo-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
    defer { try? FileManager.default.removeItem(at: dir) }
    let fifo = dir.appendingPathComponent("pod-rootd").path
    #expect(mkfifo(fifo, 0o600) == 0)
    let backend = FakeBackend()
    backend.source = ScratchSource(directory: dir.path)
    backend.updates.candidates = [fifo: .init(version: "99")]
    let rig = Rig(backend: backend, updates: true)
    rig.send(.lidHold(seconds: hour))
    rig.engine.tick()
    heatWhileLooking(rig)
    await settled { !rig.engine.updating }
    #expect(!rig.engine.restartForUpdate)
    #expect(rig.backend.installedVersion == "56")
    #expect(try FileManager.default.contentsOfDirectory(atPath: dir.path) == ["pod-rootd"])
}

@Test("a candidate on a volume that isn't local APFS or HFS (network, FUSE) is passed over")
func updateNonLocalCandidate() async throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pod-rootd-mount-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
    defer { try? FileManager.default.removeItem(at: dir) }
    let candidate = dir.appendingPathComponent("pod-rootd").path
    FileManager.default.createFile(atPath: candidate, contents: Data("new helper".utf8))
    let backend = FakeBackend()
    backend.source = ScratchSource(directory: dir.path, local: false)
    backend.updates.candidates = [candidate: .init(version: "99")]
    let rig = Rig(backend: backend, updates: true)
    rig.send(.lidHold(seconds: hour))
    rig.engine.tick()
    heatWhileLooking(rig)
    await settled { !rig.engine.updating }
    #expect(!rig.engine.restartForUpdate)
    #expect(try FileManager.default.contentsOfDirectory(atPath: dir.path) == ["pod-rootd"])
}

@Test("helper.uninstall restores the defaults, removes the package's files and then boots the job out")
func uninstallRestoresThenRemoves() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.send(.sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: true))
    let reply = rig.send(.helperUninstall)
    #expect(rig.changed(reply) == true)
    #expect(rig.backend.fans.allSatisfy { !$0.manual })
    #expect(rig.backend.sysctls[.maxVnodes] == 263_168)
    #expect(!rig.backend.installed)
    #expect(!rig.backend.bootedOut)  // the reply goes out first
    #expect(rig.engine.nextTickDelay == 0)
    let saves = rig.store.saves
    rig.engine.tick()
    #expect(rig.backend.bootedOut)
    #expect(rig.store.saves == saves)  // the state file went with the uninstall
    // the Electron process can't, the CLI only approved
    let other = Rig()
    #expect(other.send(.helperUninstall, as: .app).refusal == .verbNotAllowed(verb: "helper.uninstall", caller: .app))
    #expect(other.engine.handle(Request(.helperUninstall), from: .cli, session: SessionID(3)).refusal
        == .needsApproval(verb: "helper.uninstall"))
    #expect(other.backend.installed)
}

@Test("an uninstall whose restore fails removes nothing")
func uninstallKeepsFilesWhenRestoreFails() {
    let rig = Rig()
    rig.send(.fansSet(mode: fan50))
    rig.backend.failing = ["applyFans"]
    guard case .refused(.failed) = rig.send(.helperUninstall).outcome else {
        Issue.record("expected a failure")
        return
    }
    #expect(rig.backend.installed)
    #expect(!rig.engine.uninstalling)
}
