// acc-cored: claude-acc's resident loops in one native, event-driven daemon (docs/acc-cored.md).
// This front holds the commands; the work lives in AccCore so tests and the parity harness reach it.
import AccCore
import Darwin
import Foundation

let usage = """
    usage: acc-cored measure [--seconds N] [--json] [LABEL...]   what claude-acc's launchd jobs cost
           acc-cored scan                 the process table as devguard reads it, JSON lines (parity)
           acc-cored sockets              listening ports and loopback links as JSON (parity)
           acc-cored regex-check [--patterns]   the guard's patterns on lines from stdin (parity)
           acc-cored cache-check [ROUNDS] [SECONDS]   the process cache against fresh reads
           acc-cored guard [--shadow FILE] [--for SECONDS] [--profile]   the dev-server guard (devguard run)
           acc-cored guard-replay F.jsonl [--show N]   the native guard tick on parity fixtures
           acc-cored guard-bench [N]      N ticks back to back, CPU per phase
           acc-cored orca-read            the host's four reads over its socket, timed
           acc-cored sched-replay F.jsonl [--show N]   the scheduler's housekeeping on parity fixtures
           acc-cored caps-watch [SECONDS]  how often the capped folders change (FSEvents), nothing run
           acc-cored run [--guard | --shadow-guard FILE] [--sched] [--for SECONDS]   the daemon
    """

func json(_ value: Any) -> String {
    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("null".utf8)
    return String(decoding: data, as: UTF8.self)
}

func fail(_ text: String, _ code: Int32 = 64) -> Never {
    FileHandle.standardError.write(Data((text + "\n").utf8))
    exit(code)
}

/// The value after `flag`, nil without the flag; a flag given last is a usage error.
func value(_ flag: String, in args: [String]) -> String? {
    guard let i = args.firstIndex(of: flag) else { return nil }
    guard i + 1 < args.count else { fail("\(flag) needs a value\n" + usage) }
    return args[i + 1]
}

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail(usage) }
args.removeFirst()

switch command {
case "measure":
    var seconds = 60.0
    var asJSON = false
    var labels: [String] = []
    var rest = args[...]
    while let word = rest.popFirst() {
        switch word {
        case "--seconds":
            guard let value = rest.popFirst().flatMap(Double.init), value > 0 else { fail(usage) }
            seconds = value
        case "--json": asJSON = true
        default: labels.append(word)
        }
    }
    Measure.run(labels: labels, seconds: seconds, json: asJSON)
case "scan":
    for row in Proc.all() {
        print(json(["pid": Int(row.pid), "ppid": Int(row.ppid), "command": row.command]))
    }
case "sockets":
    let table = SocketTable.read()
    let listen = table.listen.mapValues { $0.sorted().map(Int.init) }
    print(json([
        "listen": Dictionary(uniqueKeysWithValues: listen.map { (String($0.key), $0.value) }),
        "links": table.links.map { [Int($0.pid), $0.port] },
    ]))
case "regex-check":
    // lines on stdin: "<pattern index>\t<text>"; prints "<search> <match> <groups json>" per line
    let all: [PyRegex] = GuardPatterns.serverKinds.map(\.1) + [
        GuardPatterns.launcher, GuardPatterns.shell, GuardPatterns.agent, GuardPatterns.browser, GuardPatterns.headless,
        GuardPatterns.udidRx, GuardPatterns.simDevice, GuardPatterns.launchdSim, GuardPatterns.serveSim, GuardPatterns.simctlBooted,
    ] + GuardPatterns.families.map(\.1) + [GuardContext.host.apps, GuardContext.host.sacred]
    if args.first == "--patterns" {
        for r in all { print(r.pattern) }
        exit(0)
    }
    while let line = readLine(strippingNewline: true) {
        guard let tab = line.firstIndex(of: "\t"), let i = Int(line[..<tab]) else { continue }
        let text = String(line[line.index(after: tab)...]).pyReplace("\\n", "\n").pyReplace("\\t", "\t")
        let r = all[i]
        let groups = r.groups(text).map { PyJSON.array($0.map { $0.map(PyJSON.string) ?? .null }) } ?? .null
        print("\(r.search(text) ? 1 : 0) \(r.match(text) ? 1 : 0) \(groups.dumps())")
    }
case "cache-check":
    // the cache against fresh reads on both sides: a line that changed during the round is churn
    let rounds = args.first.flatMap(Int.init) ?? 20
    let every = args.count > 1 ? Double(args[1]) ?? 5 : 5
    let cache = ProcCache()
    var same = 0, wrong = 0, total = 0
    var examples: [String] = []
    for _ in 0..<rounds {
        let fresh = Dictionary(Proc.all().map { ($0.pid, $0.command) }, uniquingKeysWith: { a, _ in a })
        let cached = cache.rows(now: Kernel.wall())
        let fresh2 = Dictionary(Proc.all().map { ($0.pid, $0.command) }, uniquingKeysWith: { a, _ in a })
        for row in cached {
            guard let a = fresh[row.pid], let b = fresh2[row.pid], a == b else { continue }
            if a == row.command {
                same += 1
            } else {
                wrong += 1
                if examples.count < 5 { examples.append("\(row.pid): cached \(row.command.prefix(80)) | fresh \(a.prefix(80))") }
            }
        }
        total += cached.count
        Thread.sleep(forTimeInterval: every)
    }
    print(String(format: "rows same %d, wrong %d; argv reads %d of %d rows (%.1f%%)", same, wrong, cache.argvReads, total,
                 Double(cache.argvReads) * 100 / Double(max(total, 1))))
    examples.forEach { print("  " + $0) }
    exit(wrong == 0 ? 0 : 1)
case "guard-replay":
    guard let path = args.first, let data = Files.read(path) else { fail(usage) }
    let show = value("--show", in: args).flatMap { Int($0) } ?? 3
    var same = 0, different = 0, incomplete = 0, shown = 0, config = 0
    for line in String(decoding: data, as: UTF8.self).split(separator: "\n") where !line.isEmpty {
        guard let fixture = (try? PyJSON.loads(String(line)))?.object else { fail("unreadable fixture") }
        let outcome = GuardReplay.run(fixture)
        if !outcome.missing.isEmpty { incomplete += 1 }
        if outcome.configHandedOver {
            config += 1
        } else if outcome.differences.isEmpty {
            same += 1
        } else {
            different += 1
            if shown < show {
                shown += 1
                print("fixture \(same + different): \(outcome.differences.count) differences")
                for d in outcome.differences.prefix(12) { print("  " + d) }
                if !outcome.missing.isEmpty { print("  missing readings: " + outcome.missing.prefix(8).joined(separator: ", ")) }
            }
        }
    }
    print("same \(same), different \(different), fixtures with readings Python never made \(incomplete), handed to Python (config types) \(config)")
    exit(different == 0 ? 0 : 1)
case "orca-read":
    let home = ProcessInfo.processInfo.environment["HOME"] ?? "/"
    let state = GuardPaths.state(home)
    guard let client = HostResolver.resolve(python: [state + "/python", "-B", state + "/acc.py"], home: home) else { fail("no host") }
    for read in OrcaRead.allCases {
        let t0 = Kernel.wall()
        let viaSocket = client.rpc(OrcaClient.methods[read]!.0, OrcaClient.methods[read]!.1, timeout: 10)
        let t1 = Kernel.wall()
        print("\(read.rawValue): socket \(viaSocket == nil ? "failed" : "ok \(viaSocket!.dumps().utf8.count) B") in \(GuardText.fixed((t1 - t0) * 1000, 1)) ms")
    }
    exit(0)
case "guard-bench":
    let home = ProcessInfo.processInfo.environment["HOME"] ?? String(cString: getpwuid(getuid())!.pointee.pw_dir)
    let n = args.first.flatMap(Int.init) ?? 20
    let engine = GuardEngine(.init(home: home, shadow: true, shadowPath: "/tmp/acc-cored-bench-state.json"))
    _ = engine.start()
    engine.benchTick()  // warm: host, caches
    Profile.on = true
    let t0 = clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID)
    for _ in 0..<n { engine.benchTick() }
    let t1 = clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID)
    print("ticks \(n), process CPU \(GuardText.fixed(Double(t1 - t0) / 1e6 / Double(n), 3)) ms/tick")
    print(Profile.report(ticks: n))
    exit(0)
case "guard":
    let home = ProcessInfo.processInfo.environment["HOME"] ?? String(cString: getpwuid(getuid())!.pointee.pw_dir)
    let shadow = value("--shadow", in: args)
    let seconds = value("--for", in: args).flatMap { Double($0) }
    let engine = GuardEngine(.init(home: home, shadow: shadow != nil, shadowPath: shadow))
    Profile.on = args.contains("--profile")
    guard engine.start() else {
        print("strażnik już działa")
        exit(0)
    }
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let stop = { (code: Int32) in
        engine.stop()
        if Profile.on {
            let cpu = (Double(clock_gettime_nsec_np(CLOCK_PROCESS_CPUTIME_ID)) - Profile.resetAt) / 1e6
            let ticks = max(engine.ticks - 1, 1)
            print("ticks after the first \(ticks), handovers \(engine.handovers), process CPU since \(GuardText.fixed(cpu, 1)) ms (\(GuardText.fixed(cpu / Double(ticks), 2)) ms/tick)")
            print(Profile.report(ticks: ticks))
        }
        exit(code)
    }
    let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    term.setEventHandler { stop(0) }
    term.resume()
    let int = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    int.setEventHandler { stop(0) }
    int.resume()
    if let seconds { DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { stop(0) } }
    dispatchMain()
case "sched-replay":
    guard let path = args.first, let data = Files.read(path) else { fail(usage) }
    let show = value("--show", in: args).flatMap { Int($0) } ?? 3
    var same = 0, different = 0, shown = 0
    for line in String(decoding: data, as: UTF8.self).split(separator: "\n") where !line.isEmpty {
        guard let fixture = (try? PyJSON.loads(String(line)))?.object else { fail("unreadable fixture") }
        let diffs = Sched.replay(fixture)
        if diffs.isEmpty {
            same += 1
        } else {
            different += 1
            if shown < show {
                shown += 1
                print("fixture \(same + different): \(diffs.count) differences")
                for d in diffs.prefix(12) { print("  " + d) }
            }
        }
    }
    print("same \(same), different \(different)")
    exit(different == 0 ? 0 : 1)
case "caps-watch":
    let home = ProcessInfo.processInfo.environment["HOME"] ?? String(cString: getpwuid(getuid())!.pointee.pw_dir)
    let seconds = args.first.flatMap { Double($0) } ?? 300
    let (prefixes, changes) = CapsWatch.observe(home: home, seconds: seconds)
    print("watching", prefixes.map { $0.pyReplace(home, "~") })
    print("seconds with a change: \(changes.count) of \(Int(seconds)); first \(changes.prefix(20))")
case "run":
    // the daemon: the parts asked for, and the control socket ($STATE/acc-cored.sock)
    let home = ProcessInfo.processInfo.environment["HOME"] ?? String(cString: getpwuid(getuid())!.pointee.pw_dir)
    let state = GuardPaths.state(home)
    let shadow = value("--shadow-guard", in: args)
    let seconds = value("--for", in: args).flatMap { Double($0) }
    var guardEngine: GuardEngine?
    if args.contains("--guard") || shadow != nil {
        let engine = GuardEngine(.init(home: home, shadow: shadow != nil, shadowPath: shadow))
        guard engine.start() else {
            print("strażnik już działa")
            exit(0)
        }
        guardEngine = engine
    }
    var schedEngine: SchedEngine?
    if args.contains("--sched") {
        let engine = SchedEngine(stateDir: state)
        if let guardEngine { engine.snapshot = { guardEngine.latestSnapshot() } }
        engine.start()
        schedEngine = engine
    }
    let guardRunning = guardEngine
    let schedRunning = schedEngine
    let control = ControlSocket(path: state + "/acc-cored.sock") { request in
        switch request["op"]?.string {
        case "sched.final":
            return schedRunning?.final(request["id"]?.string ?? "") ?? .object(PyObject([("ok", false)]))
        case "ping":
            return .object(PyObject([
                ("ok", true), ("pid", .int(Int(getpid()))), ("guard", .bool(guardRunning != nil)), ("sched", .bool(schedRunning != nil)),
            ]))
        default:
            return .object(PyObject([("ok", false), ("error", "unknown op")]))
        }
    }
    _ = control.start()
    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let stopAll = { (code: Int32) in
        control.stop()
        schedRunning?.stop()
        guardRunning?.stop()
        exit(code)
    }
    let term = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    term.setEventHandler { stopAll(0) }
    term.resume()
    let int = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    int.setEventHandler { stopAll(0) }
    int.resume()
    if let seconds { DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { stopAll(0) } }
    dispatchMain()
case "-h", "--help", "help":
    print(usage)
default:
    fail(usage)
}
