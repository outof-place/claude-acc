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

// MARK: The self-update's copy

private func scratch() throws -> URL {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pod-rootd-stage-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o755])
    return dir
}

@Test("staging copies a regular file next to the helper as .<name>.new, 0755, and the rename puts it in place")
func stageAndInstall() throws {
    let dir = try scratch()
    defer { try? FileManager.default.removeItem(at: dir) }
    let source = dir.appendingPathComponent("candidate").path
    FileManager.default.createFile(atPath: source, contents: Data("new helper".utf8))
    let helpers = dir.appendingPathComponent("PrivilegedHelperTools")
    try FileManager.default.createDirectory(at: helpers, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o755])
    let installed = helpers.appendingPathComponent("codes.pod.app.rootd").path
    FileManager.default.createFile(atPath: installed, contents: Data("old helper".utf8))

    let staged = try HelperFiles.stage(source, into: helpers.path, name: "codes.pod.app.rootd")
    #expect(staged == helpers.path + "/.codes.pod.app.rootd.new")
    #expect(FileManager.default.contents(atPath: staged) == Data("new helper".utf8))
    #expect(try FileManager.default.attributesOfItem(atPath: staged)[.posixPermissions] as? Int == 0o755)
    // the candidate changing after the copy changes nothing in it
    FileManager.default.createFile(atPath: source, contents: Data("swapped".utf8))
    #expect(FileManager.default.contents(atPath: staged) == Data("new helper".utf8))
    // a leftover copy from an earlier try is replaced, not appended to
    _ = try HelperFiles.stage(source, into: helpers.path, name: "codes.pod.app.rootd")
    #expect(FileManager.default.contents(atPath: staged) == Data("swapped".utf8))
    try HelperFiles.install(staged, as: installed)
    #expect(FileManager.default.contents(atPath: installed) == Data("swapped".utf8))
    #expect(!FileManager.default.fileExists(atPath: staged))
}

@Test("staging refuses a link, a folder, a file over the limit and a directory others can write")
func stageRefuses() throws {
    let dir = try scratch()
    defer { try? FileManager.default.removeItem(at: dir) }
    let real = dir.appendingPathComponent("real").path
    FileManager.default.createFile(atPath: real, contents: Data(repeating: 1, count: 4096))
    let link = dir.appendingPathComponent("link").path
    try FileManager.default.createSymbolicLink(atPath: link, withDestinationPath: real)
    let into = dir.appendingPathComponent("into")
    try FileManager.default.createDirectory(at: into, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o755])

    #expect(throws: BackendError.self) { try HelperFiles.stage(link, into: into.path, name: "h") }
    #expect(throws: BackendError.self) { try HelperFiles.stage(dir.path, into: into.path, name: "h") }
    #expect(throws: BackendError.self) { try HelperFiles.stage(real, into: into.path, name: "h", maxBytes: 4095) }
    try FileManager.default.setAttributes([.posixPermissions: 0o777], ofItemAtPath: into.path)
    #expect(throws: BackendError.self) { try HelperFiles.stage(real, into: into.path, name: "h") }
    #expect(try FileManager.default.contentsOfDirectory(atPath: into.path).isEmpty)
    // a staged name that is a link (planted before the copy) is removed, never written through
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: into.path)
    let decoy = dir.appendingPathComponent("decoy").path
    FileManager.default.createFile(atPath: decoy, contents: Data("keep".utf8))
    try FileManager.default.createSymbolicLink(atPath: into.path + "/.h.new", withDestinationPath: decoy)
    _ = try HelperFiles.stage(real, into: into.path, name: "h")
    #expect(FileManager.default.contents(atPath: decoy) == Data("keep".utf8))
}

// MARK: Root's rename in the user's home

@Test("migrate's rename of spotlight-exclusions.json stays in its folder and follows no link on the way")
func userFileRename() throws {
    let home = try scratch()
    defer { try? FileManager.default.removeItem(at: home) }
    let folder = home.appendingPathComponent(".local/share/claude-acc")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let file = folder.appendingPathComponent("spotlight-exclusions.json").path
    FileManager.default.createFile(atPath: file, contents: Data("[]".utf8))
    let parts = [".local", "share", "claude-acc"]

    try UserFiles.rename(in: home.path, folder: parts, from: "spotlight-exclusions.json",
                         to: "spotlight-exclusions.json.migrated", owner: getuid())
    #expect(!FileManager.default.fileExists(atPath: file))
    #expect(FileManager.default.fileExists(atPath: file + ".migrated"))
    // someone else's file, or a link in its place, is left alone
    #expect(throws: BackendError.self) {
        try UserFiles.rename(in: home.path, folder: parts, from: "spotlight-exclusions.json.migrated",
                             to: "spotlight-exclusions.json", owner: getuid() + 1)
    }
    try FileManager.default.createSymbolicLink(atPath: file, withDestinationPath: "/etc/hosts")
    #expect(throws: BackendError.self) {
        try UserFiles.rename(in: home.path, folder: parts, from: "spotlight-exclusions.json", to: "x", owner: getuid())
    }
    // a folder on the way swapped for a link: the walk stops there
    let elsewhere = home.appendingPathComponent("elsewhere")
    try FileManager.default.createDirectory(at: elsewhere.appendingPathComponent("claude-acc"), withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: elsewhere.appendingPathComponent("claude-acc/spotlight-exclusions.json").path, contents: Data())
    try FileManager.default.removeItem(at: home.appendingPathComponent(".local/share"))
    try FileManager.default.createSymbolicLink(at: home.appendingPathComponent(".local/share"), withDestinationURL: elsewhere)
    #expect(throws: BackendError.self) {
        try UserFiles.rename(in: home.path, folder: parts, from: "spotlight-exclusions.json", to: "y", owner: getuid())
    }
    #expect(FileManager.default.fileExists(atPath: elsewhere.appendingPathComponent("claude-acc/spotlight-exclusions.json").path))
}
