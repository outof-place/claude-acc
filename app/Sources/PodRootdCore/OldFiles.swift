import Foundation

/// Deleting old files under a folder others may write in (admins can, in /Library/Logs): the walk
/// goes by directory descriptors (`openat` with O_NOFOLLOW, `fstatat`, `unlinkat`), so a folder
/// swapped for a link mid-walk can't point the delete anywhere else. Links are never followed or
/// deleted; four levels deep at most.
public enum OldFiles {
    public static func prune(_ path: String, olderThanDays days: Int64, dryRun: Bool, now: Date = .now) -> (files: Int, bytes: Int64) {
        let cutoff = Int(now.timeIntervalSince1970) - Int(days) * 86_400
        let root = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard root >= 0 else { return (0, 0) }
        var total = (files: 0, bytes: Int64(0))
        prune(root, before: cutoff, dryRun: dryRun, depth: 0, into: &total)
        return total
    }

    /// Takes ownership of `dir` and closes it.
    private static func prune(_ dir: Int32, before cutoff: Int, dryRun: Bool, depth: Int, into total: inout (files: Int, bytes: Int64)) {
        guard let stream = fdopendir(dir) else {
            close(dir)
            return
        }
        defer { closedir(stream) }
        var names: [String] = []
        while let entry = readdir(stream) {
            let name = withUnsafeBytes(of: entry.pointee.d_name) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
            if name != ".", name != ".." { names.append(name) }
        }
        for name in names {
            var info = stat()
            guard fstatat(dir, name, &info, AT_SYMLINK_NOFOLLOW) == 0 else { continue }
            switch info.st_mode & S_IFMT {
            case S_IFDIR where depth < 4:
                let child = openat(dir, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
                if child >= 0 { prune(child, before: cutoff, dryRun: dryRun, depth: depth + 1, into: &total) }
            case S_IFREG where info.st_mtimespec.tv_sec < cutoff:
                if !dryRun, unlinkat(dir, name, 0) != 0 { continue }
                total.files += 1
                total.bytes += Int64(info.st_size)
            default:
                break
            }
        }
    }
}
