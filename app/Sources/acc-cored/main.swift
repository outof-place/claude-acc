// acc-cored: claude-acc's resident loops in one native, event-driven daemon (docs/acc-cored.md).
// This front holds the commands; the work lives in AccCore so tests and the parity harness reach it.
import AccCore
import Darwin
import Foundation

let usage = """
    usage: acc-cored measure [--seconds N] [--json] [LABEL...]   what claude-acc's launchd jobs cost
    """

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
case "-h", "--help", "help":
    print(usage)
default:
    fail(usage)
}
