// When the caps (janitor's size limits on agents' output folders) can do something new. A caps run
// walks every entry with `du -sk` (17 processes and ~1.8 s of CPU per run on the author's Mac,
// every 10 minutes), but its outcome changes only when:
// - something under a capped folder changes (FSEvents on the folders, or the nearest parent that
//   exists), or janitor.json does;
// - an entry skipped as fresh (a write in progress) may go: the time the last run reported;
// - an entry skipped as busy may be free: no event says so, so such a run is followed by the
//   next one at the usual interval.
// `maxGap` bounds the rest (an event the kernel dropped is reported as such and counts as a change).
import CoreServices
import Darwin
import Dispatch

public final class CapsWatch {
    let home: String
    let queue: DispatchQueue
    let maxGap: Double
    /// what the last run left: a change since it, busy entries, the fresh deadline, when it ran
    public private(set) var dirty = true
    var busy = 0
    var retryAt: Double?
    var lastRun = 0.0
    public private(set) var prefixes: [String] = []
    var configStamp = ""
    private var stream: FSEventStreamRef?

    public init(home: String, queue: DispatchQueue, maxGap: Double = 6 * 3600) {
        self.home = home
        self.queue = queue
        self.maxGap = maxGap
    }

    deinit { stopStream() }

    var configPath: String { GuardPaths.state(home) + "/janitor.json" }

    /// The static part of each cap's glob ("~/.cache/portivo-perf/*/builds" -> ~/.cache/portivo-perf)
    static func prefix(_ pattern: String, home: String) -> String {
        var path = pattern.pyStarts("~/") ? home + String(pattern.unicodeScalars.dropFirst(1).map(Character.init)) : pattern
        if let glob = path.unicodeScalars.firstIndex(where: { "*?[".unicodeScalars.contains($0) }) {
            path = dirname(String(path.unicodeScalars[..<glob]) + "x")
        }
        return trimSlashes(path)
    }

    static func trimSlashes(_ s: String) -> String {
        var u = s.utf8[...]
        while u.count > 1, u.last == 0x2F { u = u.dropLast() }
        return String(Substring(u))
    }

    /// Whether a run is worth it now; reloads the caps and the watch when janitor.json changed.
    public func due(now: Double) -> Bool {
        let stamp = Files.stamp(configPath).map { "\($0)" } ?? "-"
        if stamp != configStamp {
            configStamp = stamp
            let caps = Files.json(configPath, fallback: .null)["caps"]?.array ?? []
            // FSEvents reports real paths (/private/tmp, not /tmp)
            prefixes = caps.compactMap { $0["path"]?.string }.map { Files.realpath(Self.prefix($0, home: home)) }
            restartStream()
            dirty = true
        }
        if prefixes.isEmpty { return false }  // check_caps returns at once without caps
        return dirty || busy > 0 || (retryAt.map { $0 <= now } ?? false) || now - lastRun >= maxGap
    }

    /// A run starts: changes from here on count for the next one.
    public func started(now: Double) {
        dirty = false
        lastRun = now
    }

    /// The run's report (`devguard caps`'s JSON line); a run that said nothing counts as busy.
    func finished(_ report: PyObject?) {
        guard let report else {
            busy = 1
            retryAt = nil
            return
        }
        busy = report["busy"]?.int ?? 0
        retryAt = report["retry_at"]?.double
    }

    /// `acc-cored caps-watch`: the seconds (from the start) in which something under the capped
    /// folders changed, over `seconds`; nothing is run.
    public static func observe(home: String, seconds: Double) -> (prefixes: [String], changes: [Int]) {
        let queue = DispatchQueue(label: "acc-cored.caps-watch")
        let watch = CapsWatch(home: home, queue: queue)
        let start = Kernel.wall()
        queue.sync {
            _ = watch.due(now: start)
            watch.started(now: start)
        }
        var changes: [Int] = []
        while Kernel.wall() - start < seconds {
            sleep(1)
            queue.sync {
                if watch.dirty {
                    changes.append(Int(Kernel.wall() - start))
                    watch.started(now: Kernel.wall())
                }
            }
        }
        return (queue.sync { watch.prefixes }, changes)
    }

    // MARK: FSEvents

    func restartStream() {
        stopStream()
        // the nearest existing directory of each prefix: a folder made later shows up as an event there
        var roots: [String] = []
        for p in prefixes {
            var dir = p
            while dir != "/", !Files.isDirectory(dir) { dir = dirname(dir) }
            if !roots.contains(dir) { roots.append(dir) }
        }
        guard !roots.isEmpty else { return }
        var context = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, flags, _ in
            guard let info else { return }
            let watch = Unmanaged<CapsWatch>.fromOpaque(info).takeUnretainedValue()
            let list = paths.assumingMemoryBound(to: UnsafePointer<CChar>.self)
            for i in 0..<count {
                watch.event(String(cString: list[i]), flags: flags[i])
            }
        }
        let cfPaths = roots as CFArray
        guard let s = FSEventStreamCreate(
            kCFAllocatorDefault, callback, &context, cfPaths, FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0,
            FSEventStreamCreateFlags(kFSEventStreamCreateFlagNone))
        else {
            dirty = true
            return
        }
        FSEventStreamSetDispatchQueue(s, queue)
        if !FSEventStreamStart(s) {
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
            dirty = true
            return
        }
        stream = s
    }

    func stopStream() {
        guard let s = stream else { return }
        FSEventStreamStop(s)
        FSEventStreamInvalidate(s)
        FSEventStreamRelease(s)
        stream = nil
    }

    /// One event: a change under a capped folder, or above it (a folder on the way made or
    /// removed), or a sign that events were lost.
    func event(_ path: String, flags: FSEventStreamEventFlags) {
        let lost = FSEventStreamEventFlags(
            kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped
                | kFSEventStreamEventFlagRootChanged)
        if flags & lost != 0 {
            dirty = true
            return
        }
        let p = Self.trimSlashes(path)
        for prefix in prefixes where p == prefix || p.pyStarts(prefix + "/") || prefix.pyStarts(p + "/") {
            dirty = true
            return
        }
    }
}
