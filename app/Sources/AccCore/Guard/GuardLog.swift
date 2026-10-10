// devguard.log and macOS notifications, as janitor.log and janitor.notify write them.
import Darwin
import Foundation

public enum GuardLog {
    nonisolated(unsafe) public static var path: String? = nil
    nonisolated(unsafe) public static var notifications = true
    /// The replay's capture: ("log", line) and ("notify", title + "\n" + text) instead of writing
    nonisolated(unsafe) public static var capture: [(String, String)]? = nil
    /// janitor.LOG_MAX_BYTES: past it the log keeps its last 1000 lines
    static let maxBytes = 512 * 1024

    /// "2026-10-10 21:08:22  line"
    public static func write(_ line: String) {
        if capture != nil {
            capture!.append(("log", line))
            return
        }
        guard let path else { return }
        var st = stat()
        if stat(path, &st) == 0, st.st_size > maxBytes, let data = Files.read(path) {
            let lines = String(decoding: data, as: UTF8.self).split(separator: "\n", omittingEmptySubsequences: false)
            let tail = lines.suffix(1001).joined(separator: "\n")
            _ = Files.writeAtomic(path, tail)
        }
        var now = time(nil)
        var tm = tm()
        localtime_r(&now, &tm)
        var buffer = [CChar](repeating: 0, count: 32)
        strftime(&buffer, buffer.count, "%Y-%m-%d %H:%M:%S", &tm)
        let text = cText(buffer) + "  " + line + "\n"
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { return }
        _ = Array(text.utf8).withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
    }

    /// janitor.notify: osascript with the text and title as the script's arguments
    public static func notify(_ title: String, _ text: String) {
        if capture != nil {
            capture!.append(("notify", title + "\n" + text))
            return
        }
        guard notifications else { return }
        _ = Spawn.output(
            ["osascript", "-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run", "--",
             text, title], timeout: 10)
    }
}

/// device.plist names of simulators (they don't change while a device exists).
public final class SimulatorNames: @unchecked Sendable {
    public static let shared = SimulatorNames()
    private var names: [String: String] = [:]
    private let lock = NSLock()

    public func name(_ udid: String, home: String) -> String? {
        let path = home + "/Library/Developer/CoreSimulator/Devices/" + udid + "/device.plist"
        lock.lock()
        defer { lock.unlock() }
        if let known = names[path] { return known }
        guard let data = FileManager.default.contents(atPath: path),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any] else { return nil }
        let name = (plist["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? udid
        names[path] = name
        return name
    }
}
