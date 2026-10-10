import Foundation
import Testing
@testable import PodRootdCore
import PodRootdProtocol

// The state file and what the tools print, with outputs taken from an M4 Max on 2026-10-10.

@Test("the state file round-trips, is private, and is never written through a link")
func stateFile() throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pod-rootd-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: dir) }
    let path = dir.appendingPathComponent("state.json").path
    let store = FileStateStore(path: path)
    #expect(store.load() == nil)
    var state = HelperState()
    state.fans = fan50
    state.sysctls["kern.maxvnodes"] = SysctlRecord(original: 263_168, value: 786_432, persist: true, boot: 1)
    try store.save(state)
    #expect(store.load() == state)
    let mode = try FileManager.default.attributesOfItem(atPath: path)[.posixPermissions] as? Int
    #expect(mode == 0o600)
    let dirMode = try FileManager.default.attributesOfItem(atPath: dir.path)[.posixPermissions] as? Int
    #expect(dirMode == 0o700)

    // a link planted where the file goes: the rename replaces the link, the target stays untouched
    let target = dir.appendingPathComponent("elsewhere").path
    FileManager.default.createFile(atPath: target, contents: Data("keep".utf8))
    try FileManager.default.removeItem(atPath: path)
    try FileManager.default.createSymbolicLink(atPath: path, withDestinationPath: target)
    #expect(store.load() == nil)
    try store.save(state)
    #expect(try String(contentsOfFile: target, encoding: .utf8) == "keep")
    #expect(store.load() == state)
}

@Test("pmset -g custom: powermode per power source")
func pmsetCustom() {
    let text = """
        Battery Power:
         Sleep On Power Button 1
         powermode            0
         standby              1
        AC Power:
         Sleep On Power Button 1
         powermode            2
         womp                 1
        """
    #expect(SystemText.powerModes(text) == [.battery: .automatic, .ac: .high])
    #expect(SystemText.powerModes("AC Power:\n displaysleep 10\n") == [:])
}

@Test("ifconfig -v: the tbr limit in kb/s, nothing without one")
func ifconfigTbr() {
    let limited = """
        \tscheduler: FQ_CODEL (driver managed)
        \tuplink rate: 33.47 Mbps [eff] / 33.47 Mbps [tbr] / 358.56 Mbps [max]
        \tdownlink rate: 180.07 Mbps [eff] / 358.56 Mbps [max]
        """
    #expect(SystemText.tbr(limited) == 33_470)
    #expect(SystemText.tbr("\tuplink rate: 650.00 Kbps [eff] / 650.00 Kbps [tbr] / 1.00 Gbps [max]") == 650)
    #expect(SystemText.tbr("\tuplink rate: 1.20 Gbps [eff] / 1.20 Gbps [tbr] / 2.50 Gbps [max]") == 1_200_000)
    #expect(SystemText.tbr("\tuplink rate: 180.07 Mbps [eff] / 358.56 Mbps [max]") == nil)
    #expect(SystemText.tbr("") == nil)
}

@Test("the policy: system verbs for Pod Menu and the CLI only, the rest for all three")
func verbPolicy() {
    let policy = VerbPolicy.standard
    for kind in VerbKind.allCases {
        #expect(policy.permits(kind, for: .menu))
        #expect(policy.permits(kind, for: .cli))
        #expect(policy.permits(kind, for: .app) == (kind != .system))
    }
    #expect(Verb.restoreDefaults.kind == .system)
    #expect(Verb.legacyMigrate.kind == .system)
    #expect(Verb.fansSet(mode: .auto).kind == .fans)
    #expect(Verb.lidHold(seconds: LidSeconds(60)!).kind == .power)
}

@Test("old reports go, new ones and everything behind a link stay; a dry run only counts")
func pruneOldFiles() throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("pod-rootd-prune-\(UUID().uuidString)")
    let outside = fm.temporaryDirectory.appendingPathComponent("pod-rootd-outside-\(UUID().uuidString)")
    defer {
        try? fm.removeItem(at: root)
        try? fm.removeItem(at: outside)
    }
    let old = Date.now.addingTimeInterval(-40 * 86_400)
    try fm.createDirectory(at: root.appendingPathComponent("Retired"), withIntermediateDirectories: true)
    try fm.createDirectory(at: outside, withIntermediateDirectories: true)
    for (path, date) in [("a.ips", old), ("Retired/b.diag", old), ("fresh.ips", Date.now)] {
        let file = root.appendingPathComponent(path).path
        fm.createFile(atPath: file, contents: Data(repeating: 1, count: 100))
        try fm.setAttributes([.modificationDate: date], ofItemAtPath: file)
    }
    let victim = outside.appendingPathComponent("keep.ips").path
    fm.createFile(atPath: victim, contents: Data("x".utf8))
    try fm.setAttributes([.modificationDate: old], ofItemAtPath: victim)
    try fm.createSymbolicLink(atPath: root.appendingPathComponent("link").path, withDestinationPath: outside.path)
    try fm.createSymbolicLink(atPath: root.appendingPathComponent("file-link.ips").path, withDestinationPath: victim)

    let dry = OldFiles.prune(root.path, olderThanDays: 30, dryRun: true)
    #expect(dry.files == 2)
    #expect(dry.bytes == 200)
    #expect(fm.fileExists(atPath: root.appendingPathComponent("a.ips").path))

    let real = OldFiles.prune(root.path, olderThanDays: 30, dryRun: false)
    #expect(real.files == 2)
    #expect(!fm.fileExists(atPath: root.appendingPathComponent("a.ips").path))
    #expect(!fm.fileExists(atPath: root.appendingPathComponent("Retired/b.diag").path))
    #expect(fm.fileExists(atPath: root.appendingPathComponent("fresh.ips").path))
    #expect(fm.fileExists(atPath: victim))
    // a link as the root itself is refused
    #expect(OldFiles.prune(root.appendingPathComponent("link").path, olderThanDays: 30, dryRun: false).files == 0)
    #expect(fm.fileExists(atPath: victim))
}
