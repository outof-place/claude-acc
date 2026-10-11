// Files and folders watched for changes, for perf keep. A source per path sees writes in place; one
// per folder sees what replaces, makes or removes its entries (an atomic rename lands there); a
// missing folder is waited for from its nearest existing parent. A path counts as changed only when
// its stamp (inode, mtime in ns, size) moved, so a folder busy with other files costs a stat per
// watched path in it and nothing more. Everything runs on `queue`.
import Darwin
import Dispatch

final class PathWatch: @unchecked Sendable {
    let queue: DispatchQueue
    /// the paths whose stamp moved, called on `queue`
    var changed: ([String]) -> Void = { _ in }
    private(set) var paths: Set<String> = []
    var stamps: [String: String] = [:]
    var fileSources: [String: DispatchSourceFileSystemObject] = [:]
    var dirSources: [String: DispatchSourceFileSystemObject] = [:]
    var parentSources: [String: (parent: String, source: DispatchSourceFileSystemObject)] = [:]

    init(queue: DispatchQueue) {
        self.queue = queue
    }

    /// Watches exactly `wanted` (on `queue`). A new path takes its stamp now: only a later change counts.
    func set(_ wanted: Set<String>) {
        let dirs = Set(wanted.map(dirname))
        for dir in Array(dirSources.keys) where !dirs.contains(dir) {
            dirSources[dir]?.cancel()
            dirSources[dir] = nil
        }
        for dir in Array(parentSources.keys) where !dirs.contains(dir) {
            parentSources[dir]?.source.cancel()
            parentSources[dir] = nil
        }
        for path in Array(fileSources.keys) where !wanted.contains(path) {
            fileSources[path]?.cancel()
            fileSources[path] = nil
        }
        for path in paths.subtracting(wanted) { stamps[path] = nil }
        for path in wanted.subtracting(paths) { stamps[path] = Self.stamp(path) }
        paths = wanted
        for dir in dirs where dirSources[dir] == nil && parentSources[dir] == nil { armDir(dir) }
        for path in wanted where fileSources[path] == nil { watchFile(path) }
    }

    func stop() {
        set([])
    }

    /// A path's stamp without reading it (a link is followed, as the watch's open() does); "" when
    /// it's missing.
    static func stamp(_ path: String) -> String {
        var st = stat()
        guard stat(path, &st) == 0 else { return "" }
        return "\(st.st_ino):\(Int64(st.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(st.st_mtimespec.tv_nsec)):\(st.st_size)"
    }

    /// The candidates whose stamp moved go to `changed`; one that moved is watched again, since a
    /// replaced or new file is another inode than the old descriptor sees.
    func check(_ candidates: [String]) {
        var moved: [String] = []
        for path in candidates where paths.contains(path) {
            let now = Self.stamp(path)
            guard now != stamps[path] else { continue }
            stamps[path] = now
            moved.append(path)
            fileSources[path]?.cancel()
            fileSources[path] = nil
            watchFile(path)
        }
        if !moved.isEmpty { changed(moved) }
    }

    /// A folder's source. A missing folder is waited for from its nearest existing parent, and every
    /// (re)arm compares its paths: the folder made, replaced or moved back holds other files.
    func armDir(_ dir: String) {
        dirSources[dir]?.cancel()
        dirSources[dir] = nil
        parentSources[dir]?.source.cancel()
        parentSources[dir] = nil
        if let source = watch(dir, [.write, .delete, .rename], { [weak self] events in
            guard let self else { return }
            if !events.isDisjoint(with: [.delete, .rename]) {
                self.armDir(dir)
            } else {
                self.check(self.paths.filter { dirname($0) == dir })
            }
        }) {
            dirSources[dir] = source
        } else {
            let parent = EventHub.nearest(dir)
            if let source = watch(parent, [.write, .delete, .rename], { [weak self] _ in
                // a folder on the way came or went: arm again (the folder itself, or a nearer parent)
                guard let self, Files.isDirectory(dir) || EventHub.nearest(dir) != parent else { return }
                self.armDir(dir)
            }) {
                parentSources[dir] = (parent, source)
            }
        }
        check(paths.filter { dirname($0) == dir })
    }

    func watchFile(_ path: String) {
        fileSources[path] = watch(path, [.write, .extend, .attrib, .delete, .rename, .link]) { [weak self] _ in
            self?.check([path])
        }
    }

    func watch(_ path: String, _ mask: DispatchSource.FileSystemEvent, _ handler: @escaping @Sendable (DispatchSource.FileSystemEvent) -> Void)
        -> DispatchSourceFileSystemObject?
    {
        // O_NONBLOCK: a FIFO in a watched place doesn't hold the queue in open()
        let fd = open(path, O_EVTONLY | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: mask, queue: queue)
        source.setEventHandler { [weak source] in handler(source?.data ?? []) }
        source.setCancelHandler { close(fd) }
        source.resume()
        return source
    }
}
