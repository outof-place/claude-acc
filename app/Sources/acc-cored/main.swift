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
    """

func json(_ value: Any) -> String {
    let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("null".utf8)
    return String(decoding: data, as: UTF8.self)
}

func fail(_ text: String, _ code: Int32 = 64) -> Never {
    FileHandle.standardError.write(Data((text + "\n").utf8))
    exit(code)
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
case "-h", "--help", "help":
    print(usage)
default:
    fail(usage)
}
