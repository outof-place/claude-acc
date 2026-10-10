import Foundation
import Testing
@testable import PodRootdCore
import PodRootdProtocol

// Verbs decode only with parameters in range: what reaches the engine is already valid.

private func decode(_ json: String) -> Request? {
    try? JSONDecoder().decode(Request.self, from: Data(json.utf8))
}

private func roundTrip(_ verb: Verb) throws -> Verb {
    try JSONDecoder().decode(Request.self, from: JSONEncoder().encode(Request(verb))).verb
}

@Test("a fan setting decodes only from 30 to 100 percent, the range fanctl set takes")
func fanPercentRange() {
    #expect(decode(#"{"version":1,"verb":{"fansSet":{"mode":{"fixed":{"_0":30}}}}}"#) != nil)
    #expect(decode(#"{"version":1,"verb":{"fansSet":{"mode":{"fixed":{"_0":100}}}}}"#) != nil)
    #expect(decode(#"{"version":1,"verb":{"fansSet":{"mode":{"fixed":{"_0":29}}}}}"#) == nil)
    #expect(decode(#"{"version":1,"verb":{"fansSet":{"mode":{"fixed":{"_0":101}}}}}"#) == nil)
    #expect(decode(#"{"version":1,"verb":{"fansSet":{"mode":{"fixed":{"_0":"50"}}}}}"#) == nil)
}

@Test("every bounded parameter refuses the value just outside its range")
func boundsAtTheEdges() {
    func check<T: BoundedValue>(_ type: T.Type) {
        #expect(T(T.allowed.lowerBound) != nil)
        #expect(T(T.allowed.upperBound) != nil)
        #expect(T(T.allowed.lowerBound - 1) == nil)
        #expect(T(T.allowed.upperBound + 1) == nil)
    }
    check(FanPercent.self)
    check(LidSeconds.self)
    check(MaxVnodes.self)
    check(GPUMegabytes.self)
    check(UplinkKbps.self)
    check(FSGuardLimitMB.self)
    check(DiagnosticAgeDays.self)
}

@Test("an interface is en0 to en999 and nothing that could be a path or a flag")
func interfaceNames() {
    for good in ["en0", "en8", "en999"] { #expect(InterfaceName(good) != nil) }
    for bad in ["", "en", "en1000", "lo0", "utun3", "en0 ", "en-1", "../en0", "-en0", "en0;rm", "bridge0", "en٣"] {
        #expect(InterfaceName(bad) == nil, "\(bad.debugDescription)")
    }
    #expect(decode(#"{"version":1,"verb":{"shaperClear":{"interface":"lo0"}}}"#) == nil)
    #expect(decode(#"{"version":1,"verb":{"shaperClear":{"interface":"en8"}}}"#)?.verb == .shaperClear(interface: InterfaceName("en8")!))
}

@Test("an unknown verb does not decode; neither does a GPU limit below 4 GB")
func unknownAndOddValues() {
    #expect(decode(#"{"version":1,"verb":{"shell":{"command":"id"}}}"#) == nil)
    #expect(decode(#"{"version":1,"verb":{"sysctlSet":{"setting":{"gpuWiredLimit":{"_0":{"megabytes":{"_0":1024}}}},"persist":true}}}"#) == nil)
    #expect(decode(#"{"version":1,"verb":{"sysctlSet":{"setting":{"gpuWiredLimit":{"_0":{"systemDefault":{}}}},"persist":false}}}"#) != nil)
}

@Test("every verb survives the wire unchanged")
func wireRoundTrip() throws {
    let verbs: [Verb] = [
        .status, .fansSet(mode: .auto), .fansSet(mode: fan50), .lidHold(seconds: LidSeconds(3600)!), .lidRelease,
        .powerMode(source: .ac, mode: .high), .sysctlSet(setting: .maxVnodes(MaxVnodes(786_432)!), persist: true),
        .sysctlSet(setting: .gpuWiredLimit(.megabytes(GPUMegabytes(40_960)!)), persist: false),
        .sysctlReset(key: .gpuWiredLimitMB), .shaperSet(interface: en0, kbps: UplinkKbps(27_000)!, scope: .session),
        .shaperClear(interface: en0), .spotlightAppsOnly, .spotlightRestore,
        .fsguardSet(enabled: true, limitMB: .standard), .launchdParkOrphans(dryRun: true),
        .logsPruneDiagnostics(olderThanDays: .standard, dryRun: false), .legacyMigrate, .legacyRollback, .restoreDefaults,
    ]
    for verb in verbs { #expect(try roundTrip(verb) == verb) }
}

@Test("a verb built in code past the range is refused by the engine too")
func uncheckedValueRefused() {
    let rig = Rig()
    let reply = rig.send(.fansSet(mode: .fixed(FanPercent(unchecked: 5))))
    #expect(reply.refusal == .invalid("a parameter is out of range"))
    #expect(rig.backend.calls.isEmpty)
}

@Test("a request that did not decode is refused, and so is another protocol version")
func badRequests() {
    let rig = Rig()
    #expect(rig.engine.handle(nil, from: .cli, session: SessionID(1)).refusal != nil)
    let old = Request(.status, version: 0)
    #expect(rig.engine.handle(old, from: .cli, session: SessionID(1)).refusal == .versionMismatch(helper: PodRootd.protocolVersion))
}

@Test("names: the plist, label and Mach service hang off Pod's bundle id")
func names() {
    #expect(PodRootd.serviceName() == "codes.pod.app.rootd")
    #expect(PodRootd.plistName() == "codes.pod.app.rootd.plist")
    #expect(PodRootd.serviceName(appIdentifier: "codes.pod.canary") == "codes.pod.canary.rootd")
    #expect(Caller.allCases.map(\.signingIdentifier) == ["codes.pod.app", "com.filip.claude-acc.menubar", "codes.pod.rootctl"])
}

@Test("the launchd plist Pod ships: label, Mach service, BundleProgram next to pod-acc-run, restart only on a crash")
func launchdPlist() throws {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("launchd/codes.pod.app.rootd.plist")
    let plist = try #require(try PropertyListSerialization.propertyList(from: Data(contentsOf: url), format: nil) as? [String: Any])
    #expect(plist["Label"] as? String == PodRootd.serviceName())
    #expect((plist["MachServices"] as? [String: Bool]) == [PodRootd.serviceName(): true])
    #expect(plist["BundleProgram"] as? String == "Contents/Resources/claude-acc/pod-rootd")
    #expect(plist["ProgramArguments"] as? [String] == ["pod-rootd"])
    #expect(plist["RunAtLoad"] as? Bool == true)
    #expect((plist["KeepAlive"] as? [String: Bool]) == ["SuccessfulExit": false])
    #expect(plist["Program"] == nil)
    // launchd spawns it only as codes.pod.rootd of Pod's team (a lightweight code requirement)
    #expect((plist["SpawnConstraint"] as? [String: String]) == [
        "team-identifier": PodRootd.teamIdentifier, "signing-identifier": PodRootd.helperIdentifier])
}
