import Foundation
import LocalAuthentication
import PodRootdClient

let usage = """
    pod-rootctl: scripts' way to pod-rootd, Pod's root helper (docs/pod-rootd.md).

      status                                    everything the helper holds, as JSON
      fans auto|<30-100>
      lid hold <60-86400>                       Stay Awake with the lid closed; the hold is this
                                                command's session, so it runs until the time is up
      lid release
      power ac|battery automatic|low|high
      sysctl set maxvnodes <n> [--persist]
      sysctl set gpu-wired-limit-mb <0|MB> [--persist]
      sysctl reset maxvnodes|gpu-wired-limit-mb
      shaper set <en N> <kbps>                  until a restart or shaper clear
      shaper clear <en N>
      shaper follow <en N>                      a limit per stdin line (kb/s, or "off"), one session:
                                                when this command ends the limit goes
      spotlight apps-only|restore
      fsguard on [--limit-mb N] | off
      launchd park-orphans [--dry-run]
      logs prune [--days N] [--dry-run]
      legacy migrate|rollback                   the five com.filip.claude-acc root daemons
      restore                                   everything back to macOS's defaults
      uninstall                                 restore, then the package's files and the job go
      service status                            install, approve (switched off in Login Items) or ready
      service install                           opens Pod's signed package in Installer
      service open-settings                     System Settings at Login Items

    Options: --json (the helper's whole reply), --app-id <bundle id> (another Pod build).
    Tier B (everything but status, fans and lid) asks for Touch ID, once per run; within 5 minutes
    the same parent (shell, script) doesn't ask again.
    Exit: 0 done, 1 refused or failed, 2 usage, 69 helper unreachable.
    """

var args = Array(CommandLine.arguments.dropFirst())

func flag(_ name: String) -> Bool {
    guard let i = args.firstIndex(of: name) else { return false }
    args.remove(at: i)
    return true
}

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let value = args[i + 1]
    args.removeSubrange(i...(i + 1))
    return value
}

func fail(_ message: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data("pod-rootctl: \(message)\n".utf8))
    exit(code)
}

func bounded<T: BoundedValue>(_ text: String?, _ type: T.Type, _ what: String) -> T {
    guard let text, let number = Int64(text), let value = T(number) else { fail("\(what) must be in \(T.allowed)") }
    return value
}

func interface(_ text: String?) -> InterfaceName {
    guard let text, let name = InterfaceName(text) else { fail("the interface is en<N>, e.g. en0") }
    return name
}

/// The Pod.app this binary sits in, if any.
func hostApp() -> URL? {
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(getpid(), &buffer, UInt32(buffer.count)) > 0 else { return nil }
    var url = URL(fileURLWithPath: String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self))
    while url.pathComponents.count > 1 {
        url.deleteLastPathComponent()
        if url.pathExtension == "app", Bundle(url: url)?.bundleIdentifier != nil { return url }
    }
    return nil
}

/// The Pod.app this binary sits in, for the helper's service name.
func hostAppIdentifier() -> String {
    hostApp().flatMap { Bundle(url: $0)?.bundleIdentifier } ?? PodRootd.appIdentifier
}

let json = flag("--json")
let persist = flag("--persist")
let dryRun = flag("--dry-run")
let appIdentifier = option("--app-id") ?? hostAppIdentifier()
let limitMB = option("--limit-mb")
let days = option("--days")

func sysctlKey(_ text: String?) -> SysctlKey {
    switch text {
    case "maxvnodes": .maxVnodes
    case "gpu-wired-limit-mb": .gpuWiredLimitMB
    default: fail("sysctl key: maxvnodes or gpu-wired-limit-mb")
    }
}

/// The verb the arguments ask for; `follow` for `shaper follow`.
enum Command {
    case verb(Verb)
    case follow(InterfaceName)
    case hold(LidSeconds)
    case openSettings
    case serviceStatus
    case install
}

func parse() -> Command {
    let word = { (i: Int) in i < args.count ? args[i] : nil }
    switch (word(0), word(1)) {
    case ("status", _): return .verb(.status)
    case ("fans", "auto"): return .verb(.fansSet(mode: .auto))
    case ("fans", let percent): return .verb(.fansSet(mode: .fixed(bounded(percent, FanPercent.self, "the fan setting"))))
    case ("lid", "hold"):
        let seconds = bounded(word(2), LidSeconds.self, "the hold")
        return .hold(seconds)
    case ("lid", "release"): return .verb(.lidRelease)
    case ("power", let source):
        guard let source = source.flatMap(PowerSource.init), let mode = PowerMode.allCases.first(where: { $0.name == word(2) })
        else { fail("power ac|battery automatic|low|high") }
        return .verb(.powerMode(source: source, mode: mode))
    case ("sysctl", "set"):
        switch sysctlKey(word(2)) {
        case .maxVnodes:
            return .verb(.sysctlSet(setting: .maxVnodes(bounded(word(3), MaxVnodes.self, "maxvnodes")), persist: persist))
        case .gpuWiredLimitMB:
            let limit: GPULimit = word(3) == "0" ? .systemDefault : .megabytes(bounded(word(3), GPUMegabytes.self, "the GPU limit"))
            return .verb(.sysctlSet(setting: .gpuWiredLimit(limit), persist: persist))
        }
    case ("sysctl", "reset"): return .verb(.sysctlReset(key: sysctlKey(word(2))))
    case ("shaper", "set"):
        return .verb(.shaperSet(
            interface: interface(word(2)), kbps: bounded(word(3), UplinkKbps.self, "the limit in kb/s"),
            scope: .untilReboot))
    case ("shaper", "clear"): return .verb(.shaperClear(interface: interface(word(2))))
    case ("shaper", "follow"): return .follow(interface(word(2)))
    case ("spotlight", "apps-only"): return .verb(.spotlightAppsOnly)
    case ("spotlight", "restore"): return .verb(.spotlightRestore)
    case ("fsguard", "on"):
        let limit = limitMB.map { bounded($0, FSGuardLimitMB.self, "--limit-mb") } ?? .standard
        return .verb(.fsguardSet(enabled: true, limitMB: limit))
    case ("fsguard", "off"): return .verb(.fsguardSet(enabled: false, limitMB: .standard))
    case ("launchd", "park-orphans"): return .verb(.launchdParkOrphans(dryRun: dryRun))
    case ("logs", "prune"):
        let age = days.map { bounded($0, DiagnosticAgeDays.self, "--days") } ?? .standard
        return .verb(.logsPruneDiagnostics(olderThanDays: age, dryRun: dryRun))
    case ("legacy", "migrate"): return .verb(.legacyMigrate)
    case ("legacy", "rollback"): return .verb(.legacyRollback)
    case ("restore", _): return .verb(.restoreDefaults)
    case ("uninstall", _): return .verb(.helperUninstall)
    case ("service", "open-settings"): return .openSettings
    case ("service", "status"): return .serviceStatus
    case ("service", "install"): return .install
    case ("-h", _), ("--help", _), ("help", _):
        print(usage)
        exit(0)
    default:
        fail("unknown command\n\n" + usage)
    }
}

let command = parse()

if case .openSettings = command {
    PodRootdService.openLoginItems()
    exit(0)
}

if case .serviceStatus = command {
    let step = PodRootdService(appIdentifier: appIdentifier).step
    print(json ? "{\"step\": \"\(step)\"}" : "\(step)")
    exit(0)
}

if case .install = command {
    // Installer.app checks the package's signature and notarization and asks for an administrator
    guard let app = hostApp() else { fail("not inside a Pod.app: open its \(PodRootd.packageName) by hand", code: 1) }
    let package = PodRootdService.package(in: app)
    guard FileManager.default.isReadableFile(atPath: package.path) else { fail("\(package.path) is missing", code: 1) }
    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    open.arguments = [package.path]
    do {
        try open.run()
        open.waitUntilExit()
    } catch {
        fail("open \(package.path): \(error)", code: 1)
    }
    exit(open.terminationStatus == 0 ? 0 : 1)
}

/// Who ran this command, for the helper's 5-minute grace: the session, the parent pid with its start
/// time (a reused pid is another parent) and the terminal. A script started from the same shell is
/// another parent, so it gets no grace from the shell's Touch ID.
func parentKey() -> String {
    let parent = getppid()
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    let started = proc_pidinfo(parent, PROC_PIDTBSDINFO, 0, &info, size) == size ? info.pbi_start_tvsec : 0
    let tty = ttyname(0).map { String(cString: $0) } ?? "-"
    return "sid=\(getsid(0)) ppid=\(parent)@\(started) tty=\(tty)"
}

/// sudo asked for a password or Touch ID before a root change; a tier B verb still does, once per run.
var authenticated = false

func authenticate(for verb: Verb) async {
    let context = LAContext()
    do {
        _ = try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "change the system: \(verb.summary)")
        authenticated = true
    } catch {
        fail("authentication: \(error.localizedDescription)", code: 1)
    }
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys, .prettyPrinted]
let compact = JSONEncoder()
compact.outputFormatting = [.sortedKeys]

func show(_ reply: Reply, verb: Verb) -> Int32 {
    if json || verb == .status {
        let value: any Encodable = verb == .status && !json ? reply.status as (any Encodable)? ?? reply : reply
        print(String(decoding: (try? encoder.encode(value)) ?? Data(), as: UTF8.self))
    }
    switch reply.outcome {
    case .refused(let refusal):
        FileHandle.standardError.write(Data("pod-rootctl: \(verb.summary): \(refusal)\n".utf8))
        return 1
    case .done(let changed, let note, let report):
        guard !json, verb != .status else { return 0 }
        print("\(verb.summary): \(changed ? "changed" : "unchanged")\(note.map { " (\($0))" } ?? "")")
        switch report {
        case .orphans(let found, let parked)?:
            for orphan in found { print("  \(parked ? "parked" : "would park") \(orphan.plist) -> \(orphan.program)") }
        case .pruned(let files, let bytes, let dry)?:
            print("  \(dry ? "would delete" : "deleted") \(files) reports, \(bytes / 1_048_576) MB")
        case .legacy(let daemons)?:
            for daemon in daemons { print("  \(daemon.rawValue)") }
        case nil:
            break
        }
        return 0
    }
}

let client: PodRootdClient
do {
    client = try PodRootdClient(appIdentifier: appIdentifier)
} catch {
    fail("cannot reach \(PodRootd.serviceName(appIdentifier: appIdentifier)): \(error)", code: 69)
}

/// Tier B goes with an approval; the helper answers needsApproval when the grace doesn't cover it,
/// and then Touch ID decides.
func send(_ verb: Verb) async -> Reply {
    let parent = parentKey()
    func attempt() async -> Reply {
        let approval = verb.tier == .b ? Approval(authenticated: authenticated, parent: parent) : nil
        do {
            return try await client.send(verb, approval: approval)
        } catch {
            fail("\(PodRootd.serviceName(appIdentifier: appIdentifier)) did not answer (not approved in Login Items, or not running): \(error)", code: 69)
        }
    }
    let reply = await attempt()
    guard case .needsApproval? = reply.refusal, !authenticated else { return reply }
    await authenticate(for: verb)
    return await attempt()
}

switch command {
case .verb(let verb):
    exit(show(await send(verb), verb: verb))

case .hold(let seconds):
    let verb = Verb.lidHold(seconds: seconds)
    let code = show(await send(verb), verb: verb)
    guard code == 0 else { exit(code) }
    // the hold is this session's: it ends at the time, on Ctrl-C, or when this process goes
    try? await Task.sleep(for: .seconds(seconds.value))
    exit(0)

case .follow(let name):
    // one line in, one compact reply out; EOF clears the limit by ending the session
    while let line = readLine() {
        let text = line.trimmingCharacters(in: .whitespaces)
        let verb: Verb
        if text == "off" || text == "0" {
            verb = .shaperClear(interface: name)
        } else if let number = Int64(text), let kbps = UplinkKbps(number) {
            verb = .shaperSet(interface: name, kbps: kbps, scope: .session)
        } else {
            FileHandle.standardError.write(Data("pod-rootctl: \(text.debugDescription): kb/s in \(UplinkKbps.allowed) or off\n".utf8))
            continue
        }
        let reply = await send(verb)
        print(String(decoding: (try? compact.encode(reply.outcome)) ?? Data(), as: UTF8.self))
        fflush(stdout)
    }
    client.close()
    exit(0)

case .openSettings, .serviceStatus, .install:
    exit(0)
}
