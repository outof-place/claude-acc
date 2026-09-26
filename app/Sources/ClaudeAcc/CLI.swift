import Foundation

struct CLIResult {
    let status: Int32
    let stdout: String
    let stderr: String

    /// Ostatnia niepusta linia wyjścia: tam skrypt pisze wynik albo powód błędu.
    var message: String {
        let lines = (stdout + "\n" + stderr).split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        let text = lines.last { !$0.isEmpty } ?? ""
        return text.hasPrefix("błąd: ") ? String(text.dropFirst(6)) : text
    }
}

/// Wywołania skryptu `accswitch.py`, tego samego, którego co 2 minuty używa launchd.
enum CLI {
    static let script = NSHomeDirectory() + "/.local/share/claude-acc/accswitch.py"
    static let logFile = NSHomeDirectory() + "/.local/share/claude-acc/switch.log"

    static func process(_ args: [String]) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [script] + args
        // aplikacja z Findera dostaje ubogie środowisko: skrypt potrzebuje USER
        // (nazwa konta w Pęku kluczy) i PATH do `claude` przy logowaniu
        var env = ProcessInfo.processInfo.environment
        env["USER"] = NSUserName()
        env["HOME"] = NSHomeDirectory()
        env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = env
        return process
    }

    static func run(_ args: [String]) async -> CLIResult {
        await run(process(args))
    }

    static func run(_ process: Process) async -> CLIResult {
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        return await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: CLIResult(status: -1, stdout: "", stderr: error.localizedDescription))
                    return
                }
                // oba strumienie czytamy równolegle i do końca, inaczej pełna rura zatrzyma skrypt
                var errData = Data()
                let group = DispatchGroup()
                group.enter()
                DispatchQueue.global().async {
                    errData = err.fileHandleForReading.readDataToEndOfFile()
                    group.leave()
                }
                let outData = out.fileHandleForReading.readDataToEndOfFile()
                group.wait()
                process.waitUntilExit()
                continuation.resume(returning: CLIResult(
                    status: process.terminationStatus,
                    stdout: String(decoding: outData, as: UTF8.self),
                    stderr: String(decoding: errData, as: UTF8.self)))
            }
        }
    }
}
