import Foundation

/// A rename root does inside the user's home (docs/pod-rootd.md, "Migration"): every folder from the
/// home down is opened with O_NOFOLLOW, the file must be the user's own regular file, and it stays in
/// its folder, so a link planted anywhere on the way can't send root's rename elsewhere.
public enum UserFiles {
    public static func rename(in home: String, folder: [String], from: String, to: String, owner: uid_t) throws {
        var dir = open(home, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dir >= 0 else { throw posix("open \(home)") }
        for name in folder {
            let next = openat(dir, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            let failure = next < 0 ? posix("open \(name) in \(home)") : nil
            close(dir)
            if let failure { throw failure }
            dir = next
        }
        defer { close(dir) }
        var info = stat()
        guard fstatat(dir, from, &info, AT_SYMLINK_NOFOLLOW) == 0 else { throw posix("stat \(from)") }
        guard info.st_mode & S_IFMT == S_IFREG, info.st_uid == owner else {
            throw BackendError("\(from) is not a regular file of uid \(owner)")
        }
        guard renameat(dir, from, dir, to) == 0 else { throw posix("rename \(from)") }
    }

    static func posix(_ what: String) -> BackendError { BackendError("\(what): \(String(cString: strerror(errno)))") }
}
