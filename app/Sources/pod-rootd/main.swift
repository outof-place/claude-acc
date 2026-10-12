import Foundation
import os
import PodRootdCore
import PodRootdProtocol
import Security

// pod-rootd: Pod's one root helper (docs/pod-rootd.md). Pod's package installs it as
// /Library/PrivilegedHelperTools/<app id>.rootd with /Library/LaunchDaemons/<app id>.rootd.plist, and
// launchd starts it at boot (RunAtLoad, to put back persisted sysctls and a fixed fan setting), on a
// connection to its Mach service, and after a crash or a self-update. With nothing to watch and no
// session for a minute it exits 0, and launchd starts it on demand again.
//
//   pod-rootd             as launchd runs it
//   pod-rootd --version   the protocol version

if CommandLine.arguments.dropFirst().first == "--version" {
    print("pod-rootd protocol \(PodRootd.protocolVersion)")
    exit(0)
}

/// Pod's bundle id, from the program's own name (`<app id>.rootd` in /Library/PrivilegedHelperTools):
/// the job's label, Mach service and plist hang off it.
func hostAppIdentifier() -> String {
    let name = (SystemBackend.ownPath().map { ($0 as NSString).lastPathComponent }) ?? ""
    let id = name.hasSuffix(".rootd") ? String(name.dropLast(".rootd".count)) : ""
    return SystemBackend.validLabel(id) != nil && id.contains(".") ? id : PodRootd.appIdentifier
}

guard getuid() == 0 else {
    FileHandle.standardError.write(Data("pod-rootd: runs as root under launchd; for scripts there is pod-rootctl\n".utf8))
    exit(77)
}

/// Root only for the signed codes.pod.rootd of Pod's team: an ad hoc or unsigned build registered by
/// mistake stops here. A swapped binary doesn't carry this check; the plist's SpawnConstraint and
/// launchd are what stand against that (docs/pod-rootd.md, "A tampered helper").
func signedAsPods() -> Bool {
    var code: SecCode?
    var requirement: SecRequirement?
    let text = PodRootd.helperRequirementText()
    guard SecCodeCopySelf([], &code) == errSecSuccess, let code,
          SecRequirementCreateWithString(text as CFString, [], &requirement) == errSecSuccess, let requirement
    else { return false }
    return SecCodeCheckValidity(code, [], requirement) == errSecSuccess
}

guard signedAsPods() else {
    Logger(subsystem: "codes.pod.rootd", category: "engine")
        .fault("refusing to run: not signed as \(PodRootd.helperIdentifier, privacy: .public) of team \(PodRootd.teamIdentifier, privacy: .public)")
    exit(0)  // a clean exit: KeepAlive restarts only after a crash
}

let appIdentifier = hostAppIdentifier()
let service = PodRootd.serviceName(appIdentifier: appIdentifier)
let directory = "/var/db/" + service
let engine = Engine(
    backend: SystemBackend(directory: directory, appIdentifier: appIdentifier),
    store: FileStateStore(path: directory + "/state.json"))
let server: Server
do {
    server = Server(engine: engine, peers: try PeerPolicy.production())
} catch {
    FileHandle.standardError.write(Data("pod-rootd: peer requirement: \(error)\n".utf8))
    exit(70)
}
engine.start()
do {
    try server.listen(service: service)
} catch {
    FileHandle.standardError.write(Data("pod-rootd: listen on \(service): \(error)\n".utf8))
    exit(69)
}

// SIGTERM (shutdown, unregister, the Login Items switch): fans back to macOS, leases released
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        MainActor.assumeIsolated {
            engine.shutdown()
            exit(0)
        }
    }
    source.resume()
    signalSources.append(source)
}

// one timer for the engine's work (fans every 2 s while fixed, the lid every 5 s while held,
// fsguard every minute) and one for the idle exit
let ticker = DispatchSource.makeTimerSource(queue: .main)
let idler = DispatchSource.makeTimerSource(queue: .main)
let idleAfter: Double = 60

func reschedule() {
    if let delay = engine.nextTickDelay {
        ticker.schedule(deadline: .now() + delay, leeway: .milliseconds(200))
    } else {
        ticker.schedule(deadline: .distantFuture)
    }
    if engine.isIdle, server.sessions == 0 {
        idler.schedule(deadline: .now() + idleAfter, leeway: .seconds(5))
    } else {
        idler.schedule(deadline: .distantFuture)
    }
}

ticker.setEventHandler {
    MainActor.assumeIsolated {
        engine.tick()
        // a newer helper is in place: exit non-zero, so KeepAlive starts it; a hold of the lid
        // stays on through the restart (Engine.lidRehold)
        if engine.restartForUpdate { exit(EX_TEMPFAIL) }
        reschedule()
    }
}
idler.setEventHandler {
    MainActor.assumeIsolated {
        guard engine.isIdle, server.sessions == 0 else { return reschedule() }
        exit(0)
    }
}
server.onActivity = { reschedule() }
// a newer helper went in from the update's queue: exit non-zero, so KeepAlive starts it
engine.onUpdateInstalled = { exit(EX_TEMPFAIL) }
ticker.resume()
idler.resume()
reschedule()
dispatchMain()
