import Foundation
import PodRootdCore
import PodRootdProtocol

// pod-rootd: Pod's one root helper (docs/pod-rootd.md). launchd starts it from
// Contents/Library/LaunchDaemons/<app id>.rootd.plist: at boot (RunAtLoad, to put back persisted
// sysctls and a fixed fan setting), on a connection to its Mach service, and after a crash. With
// nothing to watch and no session for a minute it exits 0, and launchd starts it on demand again.
//
//   pod-rootd             as launchd runs it
//   pod-rootd --version   the protocol version

if CommandLine.arguments.dropFirst().first == "--version" {
    print("pod-rootd protocol \(PodRootd.protocolVersion)")
    exit(0)
}

/// The bundle id of the Pod.app this binary sits in: the label and Mach service hang off it.
func hostAppIdentifier() -> String {
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    guard proc_pidpath(getpid(), &buffer, UInt32(buffer.count)) > 0 else { return PodRootd.appIdentifier }
    var url = URL(fileURLWithPath: String(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self))
    while url.pathComponents.count > 1 {
        url.deleteLastPathComponent()
        guard url.pathExtension == "app" else { continue }
        let info = url.appendingPathComponent("Contents/Info.plist")
        if let data = try? Data(contentsOf: info),
           let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
           let id = plist["CFBundleIdentifier"] as? String {
            return id
        }
    }
    return PodRootd.appIdentifier
}

guard getuid() == 0 else {
    FileHandle.standardError.write(Data("pod-rootd: runs as root under launchd; for scripts there is pod-rootctl\n".utf8))
    exit(77)
}

let service = PodRootd.serviceName(appIdentifier: hostAppIdentifier())
let directory = "/var/db/" + service
let engine = Engine(backend: SystemBackend(directory: directory), store: FileStateStore(path: directory + "/state.json"))
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
ticker.resume()
idler.resume()
reschedule()
dispatchMain()
