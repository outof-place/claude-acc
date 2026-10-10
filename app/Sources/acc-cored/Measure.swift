// `acc-cored measure`: what each claude-acc launchd job costs over a window, from its resource
// coalition (CPU, wakeups, spawns, writes, energy of every run and child) and, for a resident job,
// its process's footprint. The before/after numbers of acc-cored's own loops come from here.
import AccCore
import Darwin
import Foundation

enum Measure {
    struct Job {
        let label: String
        let coalition: UInt64
        let pid: pid_t?
        let runs: Int?
    }

    /// claude-acc's agents (legacy and Pod), the menu bar app and acc-cored itself, as loaded now.
    /// Pod Menu keeps the menu bar app's bundle id, so it shows up as
    /// `application.com.filip.claude-acc.menubar.<n>.<n>` when opened, or under the plain bundle id
    /// as a login item; both start with one of these.
    static let prefixes = ["com.filip.claude-acc", "codes.pod.app.acc.", "application.com.filip.claude-acc.menubar"]

    static func run(labels: [String], seconds: Double, json: Bool) {
        let jobs = (labels.isEmpty ? loaded() : labels).compactMap(job)
        let before = jobs.map { CoalitionUsage.read($0.coalition) }
        let start = Kernel.wall()
        Thread.sleep(forTimeInterval: seconds)
        let elapsed = Kernel.wall() - start
        var rows: [[String: Any]] = []
        for (job, old) in zip(jobs, before) {
            guard let old, let new = CoalitionUsage.read(job.coalition) else { continue }
            let rate = new.rate(since: old, seconds: elapsed)
            var row: [String: Any] = [
                "label": job.label, "cpu_ms_s": round3(rate.cpuMsPerSecond), "wakeups_s": round3(rate.wakeupsPerSecond),
                "spawns": rate.spawns, "written_kb_s": round3(rate.writtenKBPerSecond), "mw": round3(rate.milliwatts),
            ]
            if let pid = job.pid, let usage = Usage.of(pid) { row["footprint_mb"] = round3(Double(usage.footprint) / 1_048_576) }
            if let runs = job.runs, let now = self.job(job.label)?.runs { row["runs"] = now - runs }
            rows.append(row)
        }
        if json {
            let out: [String: Any] = ["seconds": round3(elapsed), "jobs": rows]
            let data = (try? JSONSerialization.data(withJSONObject: out, options: [.sortedKeys, .prettyPrinted])) ?? Data()
            print(String(decoding: data, as: UTF8.self))
            return
        }
        print(String(format: "window %.0f s", elapsed))
        for row in rows {
            let label = (row["label"] as! String).padding(toLength: 56, withPad: " ", startingAt: 0)
            print(label + String(
                format: " cpu %8.3f ms/s  wakeups %7.2f/s  spawns %5d  written %7.2f KB/s  %8.3f mW",
                row["cpu_ms_s"] as! Double, row["wakeups_s"] as! Double, row["spawns"] as! Int,
                row["written_kb_s"] as! Double, row["mw"] as! Double)
                + ((row["footprint_mb"] as? Double).map { String(format: "  footprint %.1f MB", $0) } ?? ""))
        }
    }

    static func round3(_ value: Double) -> Double { (value * 1000).rounded() / 1000 }

    /// Labels of the loaded jobs in the GUI domain that belong to claude-acc.
    static func loaded() -> [String] {
        launchctl(["list"]).split(separator: "\n").compactMap { line in
            let label = line.split(separator: "\t").last.map(String.init) ?? ""
            return prefixes.contains(where: label.hasPrefix) ? label : nil
        }
    }

    static func job(_ label: String) -> Job? {
        let text = launchctl(["print", "gui/\(getuid())/\(label)"])
        guard let coalition = field(text, after: "resource coalition = {", key: "ID").flatMap(UInt64.init) else { return nil }
        return Job(
            label: label, coalition: coalition, pid: field(text, after: nil, key: "pid").flatMap { pid_t($0) },
            runs: field(text, after: nil, key: "runs").flatMap { Int($0) })
    }

    /// The value of `key = value` at the top level of `launchctl print` (or inside the block opened by `after`).
    static func field(_ text: String, after block: String?, key: String) -> String? {
        var body = Substring(text)
        if let block {
            guard let range = text.range(of: block) else { return nil }
            body = text[range.upperBound...]
        }
        for line in body.split(separator: "\n") {
            let trimmed = line.drop(while: { $0 == "\t" || $0 == " " })
            // top-level keys sit one tab deep; inside a block the first match is the block's
            if block == nil, !(line.hasPrefix("\t") && !line.hasPrefix("\t\t")) { continue }
            if trimmed.hasPrefix(key + " = ") { return String(trimmed.dropFirst(key.count + 3)) }
        }
        return nil
    }

    static func launchctl(_ args: [String]) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }
}
