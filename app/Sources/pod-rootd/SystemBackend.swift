import Darwin
import Foundation
import IOKit
import IOKit.ps
import PodRootdCore
import PodRootdProtocol
import SMCKit

/// The machine as root sees it. Syscalls and IOKit where macOS has an API; four Apple tools with
/// argument vectors built from validated values where it has none (docs/pod-rootd.md): `pmset`
/// (`SleepDisabled` and `powermode` have only SPI), `ifconfig` (the tbr ioctl is private), `mdutil`
/// and `launchctl`. No shell, no path from a caller.
final class SystemBackend: Backend {
    enum Tool: String {
        case pmset = "/usr/bin/pmset"
        case ifconfig = "/sbin/ifconfig"
        case mdutil = "/usr/bin/mdutil"
        case launchctl = "/bin/launchctl"
    }

    static let spotlightConfig = "/System/Volumes/Data/.Spotlight-V100/VolumeConfiguration.plist"
    static let diagnosticReports = "/Library/Logs/DiagnosticReports"

    /// The helper's own directory (state, old plists moved aside), root only.
    let directory: String
    private let fans: Fans?
    private var power: (at: Double, modes: [PowerSource: PowerMode])?
    private lazy var capable: Bool = Self.run(.pmset, ["-g", "cap"]).output.contains("highpowermode")

    init(directory: String) {
        self.directory = directory
        fans = (try? SMC()).map(Fans.init)
    }

    // MARK: Fans

    func fanSnapshot(sensors: Bool) throws -> FanSnapshot {
        guard let fans else { throw BackendError("no AppleSMC") }
        let reading = fans.reading(sensors: sensors)
        return FanSnapshot(
            fans: reading.fans.map { .init(manual: $0.manual, target: $0.target, min: $0.min, max: $0.max) },
            hottest: sensors ? [reading.cpu, reading.gpu].compactMap(\.self).max() : nil)
    }

    func applyFans(_ mode: FanMode) throws {
        guard let fans else { throw BackendError("no AppleSMC") }
        switch mode {
        case .auto: try fans.apply(.auto)
        case .fixed(let percent): try fans.apply(.fixed(percent: Int(percent.value)))
        }
    }

    // MARK: Sysctls

    private static func size(_ name: String) throws -> Int {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0 else { throw posix("sysctl \(name)") }
        return size
    }

    func sysctl(_ key: SysctlKey) throws -> Int64 {
        var size = try Self.size(key.rawValue)
        switch size {
        case 4:
            var value: Int32 = 0
            guard sysctlbyname(key.rawValue, &value, &size, nil, 0) == 0 else { throw Self.posix("sysctl \(key.rawValue)") }
            return Int64(value)
        case 8:
            var value: Int64 = 0
            guard sysctlbyname(key.rawValue, &value, &size, nil, 0) == 0 else { throw Self.posix("sysctl \(key.rawValue)") }
            return value
        default:
            throw BackendError("sysctl \(key.rawValue): \(size) bytes")
        }
    }

    func setSysctl(_ key: SysctlKey, to value: Int64) throws {
        let ok: Bool
        switch try Self.size(key.rawValue) {
        case 4:
            guard var narrow = Int32(exactly: value) else { throw BackendError("\(key.rawValue): \(value) does not fit") }
            ok = sysctlbyname(key.rawValue, nil, nil, &narrow, 4) == 0
        default:
            var wide = value
            ok = sysctlbyname(key.rawValue, nil, nil, &wide, 8) == 0
        }
        guard ok else { throw Self.posix("sysctl \(key.rawValue)=\(value)") }
    }

    var memoryMB: Int64 {
        var bytes: UInt64 = 0
        var size = MemoryLayout<UInt64>.size
        sysctlbyname("hw.memsize", &bytes, &size, nil, 0)
        return Int64(bytes >> 20)
    }

    var bootTime: Int64 {
        var boot = timeval()
        var size = MemoryLayout<timeval>.size
        sysctlbyname("kern.boottime", &boot, &size, nil, 0)
        return Int64(boot.tv_sec)
    }

    // MARK: Power

    /// The root power domain publishes the setting, so reading it spawns nothing (fanctl `Lid`).
    func sleepDisabled() -> Bool {
        let root = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOPMrootDomain"))
        guard root != 0 else { return false }
        defer { IOObjectRelease(root) }
        let value = IORegistryEntryCreateCFProperty(root, "SleepDisabled" as CFString, kCFAllocatorDefault, 0)?
            .takeRetainedValue()
        return (value as? Bool) ?? false
    }

    func setSleepDisabled(_ on: Bool) throws {
        try Self.check(.pmset, ["-a", "disablesleep", on ? "1" : "0"])
    }

    /// `pmset -g custom`, kept 10 s: a status read must not spawn pmset each time.
    func powerMode(_ source: PowerSource) -> PowerMode? {
        let now = Date.now.timeIntervalSince1970
        if let power, now - power.at < 10 { return power.modes[source] }
        let modes = SystemText.powerModes(Self.run(.pmset, ["-g", "custom"]).output)
        power = (now, modes)
        return modes[source]
    }

    func setPowerMode(_ mode: PowerMode, on source: PowerSource) throws {
        power = nil
        try Self.check(.pmset, [source.pmsetFlag, "powermode", String(mode.rawValue)])
    }

    var highPowerCapable: Bool { capable }

    /// Running on battery with 10% or less left (fanctl `Lid`).
    func batteryLow() -> Bool {
        guard let blob = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let source = IOPSGetProvidingPowerSourceType(blob)?.takeUnretainedValue() as String?,
              source == kIOPSBatteryPowerValue,
              let list = IOPSCopyPowerSourcesList(blob)?.takeRetainedValue() as? [CFTypeRef]
        else { return false }
        for item in list {
            guard let info = IOPSGetPowerSourceDescription(blob, item)?.takeUnretainedValue() as? [String: Any],
                  let current = info[kIOPSCurrentCapacityKey] as? Int,
                  let max = info[kIOPSMaxCapacityKey] as? Int, max > 0
            else { continue }
            if current * 100 / max <= 10 { return true }
        }
        return false
    }

    func thermalSerious() -> Bool {
        ProcessInfo.processInfo.thermalState.rawValue >= ProcessInfo.ThermalState.serious.rawValue
    }

    // MARK: Upload limit

    func interfaceExists(_ interface: InterfaceName) -> Bool { if_nametoindex(interface.name) != 0 }

    func uplinkLimit(_ interface: InterfaceName) -> Int64? {
        SystemText.tbr(Self.run(.ifconfig, ["-v", interface.name]).output)
    }

    func setUplinkLimit(_ interface: InterfaceName, kbps: Int64?) throws {
        try Self.check(.ifconfig, [interface.name, "tbr", kbps.map { "\($0)Kbps" } ?? "0"])
        // ifconfig ends with 0 also when the interface did not take the limit (perf-root.sh)
        if kbps != nil, uplinkLimit(interface) == nil {
            throw BackendError("\(interface) did not take an upload limit")
        }
    }

    // MARK: Spotlight

    private func spotlightConfig() throws -> (plist: [String: Any], format: PropertyListSerialization.PropertyListFormat) {
        let data = try Self.readFile(Self.spotlightConfig, limit: 8 << 20)
        var format = PropertyListSerialization.PropertyListFormat.binary
        guard let plist = try PropertyListSerialization.propertyList(from: data, options: [], format: &format) as? [String: Any]
        else { throw BackendError("\(Self.spotlightConfig) is not a dictionary") }
        return (plist, format)
    }

    func spotlightExclusions() throws -> [String] {
        try spotlightConfig().plist["Exclusions"] as? [String] ?? []
    }

    func setSpotlightExclusions(_ list: [String]) throws {
        var (plist, format) = try spotlightConfig()
        plist["Exclusions"] = list
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: format, options: 0)
        try Self.replace(Self.spotlightConfig, with: data)
    }

    /// perf-root.sh `spotlight apps-only`: the console user's top-level folders but Applications,
    /// /Library, /opt, /usr/local, /Users/Shared, and every folder under /Applications without an app.
    func appsOnlyExclusions() throws -> [String] {
        guard let user = Self.consoleUser() else { throw BackendError("nobody at the console, so no home to exclude") }
        let names = (try? FileManager.default.contentsOfDirectory(atPath: user.home)) ?? []
        var wanted = names.sorted().filter { !$0.hasPrefix(".") && $0 != "Applications" }
            .map { user.home + "/" + $0 }.filter(Self.isPlainDirectory)
        wanted += ["/Library", "/opt", "/usr/local", "/Users/Shared"].filter(Self.isDirectory)
        wanted += Self.withoutApps("/Applications")
        return wanted
    }

    /// Is there an .app under `path`, four levels down at most, not through links?
    static func hasApp(_ path: String, depth: Int = 0) -> Bool {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []
        return names.contains { name in
            let child = path + "/" + name
            guard isPlainDirectory(child) else { return false }
            return name.hasSuffix(".app") || (depth < 3 && hasApp(child, depth: depth + 1))
        }
    }

    /// Folders under `path` with no app in them; where there is one, the folders one level down.
    static func withoutApps(_ path: String) -> [String] {
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []).sorted()
        return names.flatMap { name -> [String] in
            let child = path + "/" + name
            guard isPlainDirectory(child), !name.hasSuffix(".app") else { return [] }
            return hasApp(child) ? withoutApps(child) : [child]
        }
    }

    /// Spotlight keeps the list in memory and writes its own copy back on `mdutil -i`; a SIGKILL
    /// gives it no chance, launchd starts it again with the list on disk, then the index is rebuilt
    /// so the old entries go (perf-root.sh).
    func reloadSpotlight() {
        for pid in Self.pids() where Self.name(of: pid) == "mds" { kill(pid, SIGKILL) }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
            Self.run(.mdutil, ["-E", "/System/Volumes/Data"])
        }
    }

    // MARK: Processes

    func footprint(ofProcessNamed name: String) -> ProcessFootprint? {
        for pid in Self.pids() where Self.name(of: pid) == name {
            if let bytes = Self.footprint(pid) { return ProcessFootprint(pid: pid, name: name, bytes: bytes) }
        }
        return nil
    }

    func terminate(pid: Int32, named name: String, grace: Double) {
        guard Self.name(of: pid) == name else { return }
        kill(pid, SIGTERM)
        DispatchQueue.main.asyncAfter(deadline: .now() + grace) {
            if Self.name(of: pid) == name { kill(pid, SIGKILL) }
        }
    }

    func processes(over bytes: UInt64) -> [ProcessFootprint] {
        Self.pids().compactMap { pid in
            guard let size = Self.footprint(pid), size > bytes else { return nil }
            return ProcessFootprint(pid: pid, name: Self.name(of: pid) ?? "?", bytes: size)
        }
    }

    static func pids() -> [pid_t] {
        let count = proc_listallpids(nil, 0)
        guard count > 0 else { return [] }
        var pids = [pid_t](repeating: 0, count: Int(count) + 64)
        let found = pids.withUnsafeMutableBytes { proc_listallpids($0.baseAddress, Int32($0.count)) }
        return pids.prefix(Int(max(found, 0))).filter { $0 > 0 }
    }

    static func name(of pid: pid_t) -> String? {
        var buffer = [CChar](repeating: 0, count: 256)
        guard proc_name(pid, &buffer, UInt32(buffer.count)) > 0 else { return nil }
        return String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }

    /// phys_footprint, the number jetsam picks victims by (fsguard.py).
    static func footprint(_ pid: pid_t) -> UInt64? {
        var info = rusage_info_v0()
        let status = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(pid, RUSAGE_INFO_V0, $0) }
        }
        return status == 0 ? info.ri_phys_footprint : nil
    }

    // MARK: launchd and logs

    /// janitor-root.sh: plists whose program went with its app. launchd tries them at every boot.
    func launchdOrphans() -> [Orphan] {
        var found: [Orphan] = []
        for dir in ["/Library/LaunchDaemons", "/Library/LaunchAgents"] {
            let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []).sorted()
            for name in names where name.hasSuffix(".plist") {
                let path = dir + "/" + name
                guard Self.isRegularFile(path), let plist = try? Self.readPlist(path) else { continue }
                let program = plist["Program"] as? String ?? (plist["ProgramArguments"] as? [String])?.first
                guard let program, program.hasPrefix("/"), !FileManager.default.fileExists(atPath: program) else { continue }
                let label = (plist["Label"] as? String).flatMap(Self.validLabel) ?? String(name.dropLast(6))
                guard Self.validLabel(label) != nil, !label.hasPrefix("com.apple."), !label.hasPrefix("codes.pod.") else { continue }
                let domain = dir.hasSuffix("LaunchAgents") ? "gui/\(Self.consoleUser()?.uid ?? 0)" : "system"
                found.append(Orphan(label: label, plist: path, program: program, domain: domain))
            }
        }
        return found
    }

    func park(_ orphan: Orphan) throws {
        guard Self.validLabel(orphan.label) != nil,
              ["/Library/LaunchDaemons/", "/Library/LaunchAgents/"].contains(where: orphan.plist.hasPrefix),
              !orphan.plist.contains("/../")
        else { throw BackendError("not a plist this helper parks: \(orphan.plist)") }
        Self.run(.launchctl, ["bootout", "\(orphan.domain)/\(orphan.label)"])
        let day = Date.now.formatted(.iso8601.year().month().day())
        let parked = "/Library/launchd-disabled-\(day)"
        if mkdir(parked, 0o755) != 0, errno != EEXIST { throw Self.posix("mkdir \(parked)") }
        let target = parked + "/" + (orphan.plist as NSString).lastPathComponent
        guard rename(orphan.plist, target) == 0 else { throw Self.posix("move \(orphan.plist)") }
    }

    func pruneDiagnostics(olderThanDays days: Int64, dryRun: Bool) -> (files: Int, bytes: Int64) {
        OldFiles.prune(Self.diagnosticReports, olderThanDays: days, dryRun: dryRun)
    }

    // MARK: The five old daemons

    func legacyArguments(_ daemon: LegacyDaemon) -> [String]? {
        guard Self.isRegularFile(daemon.plistPath), let plist = try? Self.readPlist(daemon.plistPath) else { return nil }
        return plist["ProgramArguments"] as? [String] ?? []
    }

    /// The fans.json the old daemon followed (`--config` in its root-owned plist), read without
    /// following a link: `{"mode": "fixed", "percent": N}`; anything else is auto, as fanctl had it.
    func legacyFanMode(configPath: String) -> FanMode? {
        guard configPath.hasPrefix("/Users/"), configPath.hasSuffix("/.local/share/claude-acc/fans.json"),
              !configPath.contains("/../"), let data = try? Self.readFile(configPath, limit: 4096),
              let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let mode = config["mode"] as? String
        else { return nil }
        if mode == "fixed", let percent = (config["percent"] as? Int).flatMap(FanPercent.init) { return .fixed(percent) }
        return .auto
    }

    /// perf-root.sh kept the Privacy list from before apps-only in the user's state folder.
    func legacySpotlightList() -> [String]? {
        guard let user = Self.consoleUser(),
              let data = try? Self.readFile(user.home + "/.local/share/claude-acc/spotlight-exclusions.json", limit: 1 << 20),
              let list = try? JSONDecoder().decode([String].self, from: data),
              list.allSatisfy({ $0.hasPrefix("/") && !$0.contains("/../") })
        else { return nil }
        return list
    }

    func bootout(_ daemon: LegacyDaemon) throws {
        // a job that is not loaded is as good as booted out
        Self.run(.launchctl, ["bootout", "system/\(daemon.rawValue)"])
    }

    func moveLegacyPlist(_ daemon: LegacyDaemon, aside: Bool) throws {
        let dir = directory + "/legacy"
        if mkdir(directory, 0o700) != 0, errno != EEXIST { throw Self.posix("mkdir \(directory)") }
        if mkdir(dir, 0o700) != 0, errno != EEXIST { throw Self.posix("mkdir \(dir)") }
        let saved = dir + "/" + daemon.rawValue + ".plist"
        let (from, to) = aside ? (daemon.plistPath, saved) : (saved, daemon.plistPath)
        guard Self.isRegularFile(from) else { throw BackendError("no \(from)") }
        guard rename(from, to) == 0 else { throw Self.posix("move \(from)") }
        if !aside {
            chown(to, 0, 0)
            chmod(to, 0o644)
        }
    }

    func bootstrap(_ daemon: LegacyDaemon) throws {
        Self.run(.launchctl, ["bootout", "system/\(daemon.rawValue)"])
        try Self.check(.launchctl, ["bootstrap", "system", daemon.plistPath])
    }

    // MARK: Plumbing

    /// Runs one of the four tools with `posix_spawn`: no shell, a fixed PATH, stdin from /dev/null;
    /// stdout and stderr together.
    @discardableResult
    static func run(_ tool: Tool, _ arguments: [String]) -> (status: Int32, output: String) {
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else { return (-1, "") }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        posix_spawn_file_actions_adddup2(&actions, fds[1], 1)
        posix_spawn_file_actions_adddup2(&actions, fds[1], 2)
        posix_spawn_file_actions_addclose(&actions, fds[0])
        posix_spawn_file_actions_addclose(&actions, fds[1])
        let argv: [UnsafeMutablePointer<CChar>?] = ([tool.rawValue] + arguments).map { (text: String) in strdup(text) } + [nil]
        let env: [UnsafeMutablePointer<CChar>?] = ["PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL=C"].map { (text: String) in strdup(text) } + [nil]
        defer {
            argv.forEach { free($0) }
            env.forEach { free($0) }
        }
        var pid: pid_t = 0
        let spawned = posix_spawn(&pid, tool.rawValue, &actions, nil, argv, env)
        close(fds[1])
        guard spawned == 0 else {
            close(fds[0])
            return (-1, "")
        }
        var output = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let n = read(fds[0], &buffer, buffer.count)
            if n > 0 { output.append(buffer, count: n) } else if n == 0 || errno != EINTR { break }
        }
        close(fds[0])
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0, errno == EINTR {}
        let code = status & 0x7f == 0 ? (status >> 8) & 0xff : -1
        return (code, String(decoding: output, as: UTF8.self))
    }

    static func check(_ tool: Tool, _ arguments: [String]) throws {
        let result = run(tool, arguments)
        guard result.status == 0 else {
            let said = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            throw BackendError("\((tool.rawValue as NSString).lastPathComponent) \(arguments.joined(separator: " ")): exit \(result.status)\(said.isEmpty ? "" : ", " + said)")
        }
    }

    static func posix(_ what: String) -> BackendError { BackendError("\(what): \(String(cString: strerror(errno)))") }

    static func validLabel(_ label: String) -> String? {
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_")
        return !label.isEmpty && label.count <= 200 && label.allSatisfy(allowed.contains) ? label : nil
    }

    static func isPlainDirectory(_ path: String) -> Bool {
        var info = stat()
        return lstat(path, &info) == 0 && info.st_mode & S_IFMT == S_IFDIR
    }

    static func isDirectory(_ path: String) -> Bool {
        var info = stat()
        return stat(path, &info) == 0 && info.st_mode & S_IFMT == S_IFDIR
    }

    static func isRegularFile(_ path: String) -> Bool {
        var info = stat()
        return lstat(path, &info) == 0 && info.st_mode & S_IFMT == S_IFREG
    }

    /// At most `limit` bytes, never through a link.
    static func readFile(_ path: String, limit: Int) throws -> Data {
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { throw posix("open \(path)") }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        guard let data = try handle.read(upToCount: limit + 1), data.count <= limit else {
            throw BackendError("\(path) is over \(limit) bytes")
        }
        return data
    }

    static func readPlist(_ path: String) throws -> [String: Any]? {
        try PropertyListSerialization.propertyList(from: readFile(path, limit: 1 << 20), format: nil) as? [String: Any]
    }

    /// Writes next to `path` with its owner and mode, then renames over it.
    static func replace(_ path: String, with data: Data) throws {
        var info = stat()
        guard lstat(path, &info) == 0, info.st_mode & S_IFMT == S_IFREG else { throw posix("stat \(path)") }
        let tmp = path + ".pod-rootd.tmp"
        unlink(tmp)
        let fd = open(tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw posix("open \(tmp)") }
        let written = data.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        let ok = written == data.count && fchown(fd, info.st_uid, info.st_gid) == 0
            && fchmod(fd, info.st_mode & 0o7777) == 0 && fsync(fd) == 0
        close(fd)
        guard ok, rename(tmp, path) == 0 else {
            unlink(tmp)
            throw posix("write \(path)")
        }
    }

    /// Whoever owns /dev/console (perf-root.sh), with their home from the user database.
    static func consoleUser() -> (uid: uid_t, home: String)? {
        var info = stat()
        guard stat("/dev/console", &info) == 0, info.st_uid != 0 else { return nil }
        var entry = passwd()
        var result: UnsafeMutablePointer<passwd>?
        var buffer = [CChar](repeating: 0, count: 4096)
        guard getpwuid_r(info.st_uid, &entry, &buffer, buffer.count, &result) == 0, result != nil, let dir = entry.pw_dir
        else { return nil }
        let home = String(cString: dir)
        guard home.hasPrefix("/Users/") else { return nil }
        return (info.st_uid, home)
    }
}
