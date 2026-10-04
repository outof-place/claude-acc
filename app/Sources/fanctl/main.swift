import Foundation

// fanctl: reads and drives the Mac's fans through the SMC.
//
//   fanctl read [--json]          fans and the hottest CPU/GPU sensors (no root needed)
//   fanctl keys                   every SMC key with its type (diagnostics)
//   fanctl set auto|<percent>     hand the fans back to macOS, or hold them at a share of their range (root)
//   fanctl daemon --config F --state F
//                                 root LaunchDaemon: follows the mode in F, writes readings to the
//                                 state file, and never lets a chip run hot on a fixed setting

let args = Array(CommandLine.arguments.dropFirst())

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("fanctl: \(message)\n".utf8))
    exit(1)
}

func option(_ name: String) -> String? {
    args.firstIndex(of: name).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
}

do {
    let smc = try SMC()
    switch args.first ?? "read" {
    case "read":
        let reading = Fans(smc: smc).reading()
        if args.contains("--json") {
            print(String(decoding: try JSONEncoder.pretty.encode(reading), as: UTF8.self))
        } else {
            for fan in reading.fans {
                print("fan \(fan.index): \(Int(fan.rpm)) rpm (min \(Int(fan.min)), max \(Int(fan.max)), target \(Int(fan.target)), \(fan.manual ? "manual" : "auto"))")
            }
            print("cpu \(reading.cpu.map { String(format: "%.1f °C", $0) } ?? "?"), gpu \(reading.gpu.map { String(format: "%.1f °C", $0) } ?? "?")")
        }
    case "keys":
        for key in try smc.allKeys() {
            let info = try? smc.info(key)
            let value = (try? smc.number(key)).map { String(format: "%.2f", $0) } ?? ""
            print("\(key)  \(info?.type ?? "?")  \(value)")
        }
    case "set":
        guard args.count > 1 else { fail("set auto|<percent>") }
        let mode: Fans.Mode = args[1] == "auto" ? .auto : .fixed(percent: Int(args[1]) ?? -1)
        if case .fixed(let p) = mode, !(30...100).contains(p) { fail("percent must be 30-100") }
        try Fans(smc: smc).apply(mode)
    case "daemon":
        guard let config = option("--config"), let state = option("--state") else {
            fail("daemon --config <fans.json> --state <fans-state.json>")
        }
        Daemon(fans: Fans(smc: smc), config: config, state: state).run()
    default:
        fail("unknown command \(args[0])")
    }
} catch {
    fail("\(error)")
}
