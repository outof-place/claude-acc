// When the caps (janitor's size limits on agents' output folders) can do something new. A caps run
// walks every entry with `du -sk` (17 processes and ~1.8 s of CPU per run on the author's Mac,
// every 10 minutes), but its outcome changes only when:
// - something under a capped folder changes (FSEvents on the folders, or the nearest parent that
//   exists), or janitor.json does. The folders are the globs' static prefixes and the folders the
//   last run's globs matched, both as real paths: a symlinked folder lives where its link points.
//   A link right before another wildcard (caps/*/*/builds, l1 -> a worktree) is watched shallow:
//   only its own subfolders count (one made, removed or renamed), and the folders found under it
//   are watched deep. Above a watched folder only a folder on the way that came or went counts:
//   FSEvents names a folder on every file written in it, and a worktree's src/*.ts is written all
//   day;
// - free space on a watched volume dropped since the last run by at least the tightest folder's
//   headroom (what it may still grow before a run removes something), and never by less than a
//   floor (a tenth of the smallest cap, the floor itself at most 1 GiB): a file growing behind an
//   open descriptor shows up there, while FSEvents reports it only when it's closed. Free space is
//   sampled on every guard tick, and the drop counts from the highest free space since the run, so
//   space freed meanwhile doesn't hide the growth;
// - an entry skipped as fresh (a write in progress) may go: the time the last run reported;
// - an entry skipped as busy (in use, can't be moved, du failed) may be free: no event says so,
//   so such a run is followed by the next one at the usual interval.
// `maxGap` bounds the rest (an event the kernel dropped is reported as such and counts as a change).
// A cap whose path this can't place (`~user/`, `$HOME/`, relative), or a stream that didn't start,
// runs at every interval, as Python does.
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
    /// the real paths watched: the caps' static prefixes and the folders the last run matched
    public private(set) var prefixes: [String] = []
    /// `prefixes` with a slash each, made once per interval: an event allocates nothing per prefix
    var slashed: [String] = []
    /// folders watched for their own entries only (the last run's `shallow`), unless a deep prefix
    /// covers them
    public private(set) var shallow: [String] = []
    var shallowFolders: [String] = []
    /// each watched path's deepest existing folder when the last run started (itself, when it was
    /// there): an event above it counts only when the next folder on the way came or went
    var armedDeepest: [String: String] = [:]
    /// each shallow folder's subfolders and links when the last run started: an event naming it
    /// counts only when they changed
    var armedEntries: [String: [String]] = [:]
    var patterns: [String] = []
    var folders: [String] = []
    var smallestGB = 10.0
    var configStamp = ""
    /// the stream's roots (the nearest existing directory of each prefix); a change re-arms it
    var roots: [String] = []
    var rearm = false
    /// free bytes of each watched volume (by fsid) since the last run started, the highest seen
    var freeAtStart: [String: Int64] = [:]
    /// how much the tightest folder may grow before a run removes something (the last report)
    var headroom: Double?
    /// tests: the volumes' free bytes in place of statfs
    var volumes: (([String]) -> [String: Int64])?
    private var stream: FSEventStreamRef?

    public init(home: String, queue: DispatchQueue, maxGap: Double = 6 * 3600) {
        self.home = home
        self.queue = queue
        self.maxGap = maxGap
    }

    var configPath: String { GuardPaths.state(home) + "/janitor.json" }

    /// The static part of each cap's glob ("~/.cache/portivo-perf/*/builds" -> ~/.cache/portivo-perf),
    /// nil for a form expanduser or the working directory decides (`~user/`, `$HOME/`, relative).
    static func prefix(_ pattern: String, home: String) -> String? {
        var path: String
        if pattern == "~" || pattern.pyStarts("~/") {
            path = home + String(pattern.unicodeScalars.dropFirst(1).map(Character.init))
        } else if pattern.pyStarts("/") {
            path = pattern
        } else {
            return nil
        }
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

    /// Whether a run is worth it now (asked once per caps interval). Reloads the caps when
    /// janitor.json changed, and re-arms the stream when the real paths or their roots moved.
    public func due(now: Double) -> Bool {
        let stamp = Files.stamp(configPath).map { "\($0)" } ?? "-"
        if stamp != configStamp {
            configStamp = stamp
            let caps = Files.json(configPath, fallback: .null)["caps"]?.array ?? []
            patterns = caps.compactMap { $0["path"]?.string }
            smallestGB = caps.map { $0["max_gb"]?.double ?? 10 }.min() ?? 10
            folders = []
            shallowFolders = []
            dirty = true
        }
        if patterns.isEmpty {  // check_caps returns at once without caps
            stopStream()
            roots = []
            return false
        }
        let placed = patterns.map { Self.prefix($0, home: home) }
        var watched: [String] = []
        // FSEvents reports real paths (/private/tmp, not /tmp), and a folder made later in another
        // letter case is the same folder on APFS: realpath again on every interval
        for p in placed.compactMap({ $0 }).map(Files.realpath) + folders where !watched.contains(p) { watched.append(p) }
        // a folder under another watched path adds nothing but a root (and a run when it comes or goes)
        prefixes = watched.filter { p in !watched.contains { q in q != p && p.pyStarts(q + "/") } }
        slashed = prefixes.map { $0 + "/" }
        shallow = shallowFolders.filter { s in !prefixes.contains { s == $0 || s.pyStarts($0 + "/") } }
        // a path new since the last run started whose root was one already: its look from now on
        for p in prefixes + shallow where armedDeepest[p] == nil { armedDeepest[p] = Self.deepest(p) }
        for s in shallow where armedEntries[s] == nil { armedEntries[s] = Self.entries(s) }
        let wanted = Self.roots(of: prefixes + shallow)
        if wanted != roots || rearm || stream == nil {  // a stream that didn't start is tried again
            roots = wanted
            rearm = false
            restartStream()
            dirty = true
        }
        if placed.contains(where: { $0 == nil }) || stream == nil { return true }
        if spaceDropped() { dirty = true }
        return dirty || busy > 0 || (retryAt.map { $0 <= now } ?? false) || now - lastRun >= maxGap
    }

    /// A run starts: changes from here on count for the next one.
    public func started(now: Double) {
        dirty = false
        lastRun = now
        freeAtStart = volumes?(roots) ?? Self.freeSpace(roots)
        armedDeepest = [:]
        for p in prefixes + shallow { armedDeepest[p] = Self.deepest(p) }
        armedEntries = [:]
        for s in shallow { armedEntries[s] = Self.entries(s) }
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
        folders = report["folders"]?.array?.compactMap(\.string) ?? folders
        shallowFolders = report["shallow"]?.array?.compactMap(\.string) ?? shallowFolders
        headroom = report["headroom"]?.double
    }

    /// Stops the stream (on `queue`): it holds this watch, so nothing else frees it.
    public func stop() {
        stopStream()
    }

    /// One guard tick's look at free space (a statfs per root): the peak goes up, and a drop past
    /// the threshold marks the watch dirty now, not at the next interval, where space freed
    /// meanwhile would hide it.
    public func sample() {
        guard !dirty, !freeAtStart.isEmpty else { return }
        if spaceDropped() { dirty = true }
    }

    /// A drop of free space on a watched volume, from the highest free space since the run started,
    /// of at least the tightest folder's headroom and the floor (a tenth of the smallest cap, at most
    /// 1 GiB): what one file growing under a cap enough to change a run looks like.
    func spaceDropped() -> Bool {
        let floor = min(1, smallestGB / 10) * Double(1 << 30)
        // in Double: a cap of 1e10 GB or Infinity gives a headroom no Int64 holds (NaN gives the floor)
        let threshold = max(floor, headroom ?? 0)
        let now = volumes?(roots) ?? Self.freeSpace(roots)
        var dropped = false
        for (volume, free) in now {
            guard let before = freeAtStart[volume] else { continue }
            // space freed since the run would hide growth of the same size: count from the peak
            let peak = max(before, free)
            freeAtStart[volume] = peak
            if Double(peak - free) >= threshold { dropped = true }
        }
        return dropped
    }

    static func freeSpace(_ paths: [String]) -> [String: Int64] {
        var out: [String: Int64] = [:]
        for p in paths {
            var fs = statfs()
            guard statfs(p, &fs) == 0 else { continue }
            out["\(fs.f_fsid.val.0):\(fs.f_fsid.val.1)"] = Int64(fs.f_bavail) * Int64(fs.f_bsize)
        }
        return out
    }

    /// The nearest existing directory of each path: a folder made later shows up as an event there.
    static func roots(of paths: [String]) -> [String] {
        var roots: [String] = []
        for p in paths {
            let dir = deepest(p)
            if !roots.contains(dir) { roots.append(dir) }
        }
        return roots
    }

    /// The deepest existing directory of a path: the path itself when it's there.
    static func deepest(_ path: String) -> String {
        var dir = path
        while dir != "/", !Files.isDirectory(dir) { dir = dirname(dir) }
        return dir
    }

    /// A folder's subfolders and links (what a wildcard after it goes on through), hidden ones too,
    /// sorted by bytes; [] when it can't be read. A link is `name -> target`, so a link retargeted
    /// (`ln -sfn`) or swapped for a folder of its name changes the list too.
    static func entries(_ path: String) -> [String] {
        guard let dir = opendir(path) else { return [] }
        defer { closedir(dir) }
        var names: [String] = []
        while let e = readdir(dir) {
            let name = withUnsafeBytes(of: e.pointee.d_name) { String(decoding: $0.prefix(Int(e.pointee.d_namlen)), as: UTF8.self) }
            if name == "." || name == ".." { continue }
            var kind = Int32(e.pointee.d_type)
            if kind == DT_UNKNOWN {
                var st = stat()
                guard lstat(path + "/" + name, &st) == 0 else { continue }
                kind = st.st_mode & S_IFMT == S_IFDIR ? DT_DIR : st.st_mode & S_IFMT == S_IFLNK ? DT_LNK : DT_REG
            }
            if kind == DT_DIR {
                names.append(name)
            } else if kind == DT_LNK {
                var buf = [UInt8](repeating: 0, count: Int(PATH_MAX))
                let n = buf.withUnsafeMutableBufferPointer { b in
                    b.withMemoryRebound(to: CChar.self) { readlink(path + "/" + name, $0.baseAddress, $0.count) }
                }
                names.append(name + " -> " + (n >= 0 ? String(decoding: buf.prefix(n), as: UTF8.self) : ""))
            }
        }
        return names.sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }
    }

    /// Whether the folder on the way from an event's folder (`below`, with its slash) to `target` came
    /// or went since the run started: the next component is a folder now, and wasn't then, or the
    /// other way round. One stat. Unknown then: it did.
    func wayChanged(_ target: String, below: String) -> Bool {
        guard let then = armedDeepest[target] else { return true }
        let rest = target.utf8.dropFirst(below.utf8.count)
        let next = below + String(Substring(rest.prefix { $0 != 0x2F }))
        let was = then.pyEq(next) || then.pyStarts(next + "/")
        return Files.isDirectory(next) != was
    }

    /// `acc-cored caps-watch`: the seconds (from the start) in which something under the capped
    /// folders changed, over `seconds`; nothing is run. With a run's report (`acc.py devguard caps
    /// --dry-run`), the folders it matched and the shallow ones are watched too, as the guard does
    /// after that run.
    public static func observe(home: String, seconds: Double, report: PyObject? = nil)
        -> (prefixes: [String], shallow: [String], changes: [Int]) {
        let queue = DispatchQueue(label: "acc-cored.caps-watch")
        let watch = CapsWatch(home: home, queue: queue)
        let start = Kernel.wall()
        queue.sync {
            _ = watch.due(now: start)
            watch.started(now: start)
            if let report {
                watch.finished(report)
                _ = watch.due(now: start)
                watch.started(now: start)
            }
        }
        var changes: [Int] = []
        while Kernel.wall() - start < seconds {
            sleep(1)
            queue.sync {
                _ = watch.due(now: Kernel.wall())
                if watch.dirty {
                    changes.append(Int(Kernel.wall() - start))
                    watch.started(now: Kernel.wall())
                }
            }
        }
        queue.sync { watch.stop() }
        return queue.sync { (watch.prefixes, watch.shallow, changes) }
    }

    // MARK: FSEvents

    func restartStream() {
        stopStream()
        guard !roots.isEmpty else { return }
        // the stream holds the watch (retain/release): a callback never sees a freed one
        var context = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
            retain: { info in info.map { UnsafeRawPointer(Unmanaged<CapsWatch>.fromOpaque($0).retain().toOpaque()) } },
            release: { info in info.map { Unmanaged<CapsWatch>.fromOpaque($0).release() } },
            copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, flags, _ in
            guard let info else { return }
            let watch = Unmanaged<CapsWatch>.fromOpaque(info).takeUnretainedValue()
            let list = paths.assumingMemoryBound(to: UnsafePointer<CChar>.self)
            for i in 0..<count {
                watch.event(String(cString: list[i]), flags: flags[i])
            }
        }
        // WatchRoot: a root replaced or renamed away is reported (RootChanged), and re-armed
        guard let s = FSEventStreamCreate(
            kCFAllocatorDefault, callback, &context, roots as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0,
            FSEventStreamCreateFlags(kFSEventStreamCreateFlagWatchRoot))
        else { return }
        FSEventStreamSetDispatchQueue(s, queue)
        if !FSEventStreamStart(s) {
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
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

    /// One event: a change under a watched folder, or above it (a folder on the way made or
    /// removed), a shallow folder's subfolders, a root that moved, or a sign that events were lost.
    func event(_ path: String, flags: FSEventStreamEventFlags) {
        if flags & FSEventStreamEventFlags(kFSEventStreamEventFlagRootChanged) != 0 {
            dirty = true
            rearm = true
            return
        }
        if dirty { return }  // already due: a storm of events costs one flag check each
        let lost = FSEventStreamEventFlags(
            kFSEventStreamEventFlagMustScanSubDirs | kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped)
        if flags & lost != 0 {
            dirty = true
            return
        }
        let p = Self.trimSlashes(path)
        let ps = p + "/"
        for (prefix, slash) in zip(prefixes, slashed) {
            // above it: FSEvents names a folder on every file written in it, so only a folder on the
            // way that came or went counts
            if p == prefix || p.pyStarts(slash) || prefix.pyStarts(ps) && wayChanged(prefix, below: ps) {
                dirty = true
                return
            }
        }
        // a shallow folder: its subfolders made, removed or renamed (the event names the folder, also
        // for a file written in it), or a folder on the way to it; an edit deeper inside isn't the
        // caps' business
        for s in shallow {
            if p == s {
                // unknown then: they changed
                guard let then = armedEntries[s], then.elementsEqual(Self.entries(s), by: { $0.pyEq($1) }) else {
                    dirty = true
                    return
                }
            } else if s.pyStarts(ps), wayChanged(s, below: ps) {
                dirty = true
                return
            }
        }
    }
}
