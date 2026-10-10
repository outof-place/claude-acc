import Foundation

/// The self-update's file side (docs/pod-rootd.md, "Updates"). A candidate in Pod.app is copied, never
/// moved or linked, into the directory of the installed helper, which only its owner can write; the
/// signature check that follows reads that copy, so nothing can change the file between the check
/// and the rename.
public enum HelperFiles {
    public static let maxBytes = 64 << 20

    /// Copies `source` into `directory` as `.<name>.new` and returns that path. The source must be a
    /// regular file, opened without following a link, of at most `maxBytes`; the directory must
    /// belong to this process's user and be writable by no one else.
    public static func stage(_ source: String, into directory: String, name: String, maxBytes: Int = maxBytes) throws -> String {
        var dir = stat()
        guard lstat(directory, &dir) == 0, dir.st_mode & S_IFMT == S_IFDIR, dir.st_uid == geteuid(),
              dir.st_mode & 0o022 == 0
        else { throw BackendError("\(directory) is not a directory only its owner can write") }
        let input = open(source, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard input >= 0 else { throw posix("open \(source)") }
        defer { close(input) }
        var info = stat()
        guard fstat(input, &info) == 0, info.st_mode & S_IFMT == S_IFREG else { throw BackendError("\(source) is not a regular file") }
        guard info.st_size <= maxBytes else { throw BackendError("\(source) is over \(maxBytes) bytes") }
        let staged = directory + "/." + name + ".new"
        if unlink(staged) != 0, errno != ENOENT { throw posix("unlink \(staged)") }
        let output = open(staged, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o700)
        guard output >= 0 else { throw posix("open \(staged)") }
        var copied = 0
        var buffer = [UInt8](repeating: 0, count: 1 << 16)
        var ok = true
        while ok {
            let n = read(input, &buffer, buffer.count)
            if n == 0 { break }
            if n < 0 {
                if errno == EINTR { continue }
                ok = false
                break
            }
            copied += n
            // the file may grow after fstat: the limit holds for what is read
            ok = copied <= maxBytes && buffer.withUnsafeBytes { write(output, $0.baseAddress, n) } == n
        }
        ok = ok && fchmod(output, 0o755) == 0 && fsync(output) == 0
        close(output)
        guard ok else {
            unlink(staged)
            throw BackendError("copying \(source) failed")
        }
        return staged
    }

    /// The checked copy takes the installed helper's place in one rename; the running process keeps
    /// its old file until it exits.
    public static func install(_ staged: String, as target: String) throws {
        guard rename(staged, target) == 0 else {
            let error = posix("rename \(staged)")
            unlink(staged)
            throw error
        }
    }

    static func posix(_ what: String) -> BackendError { BackendError("\(what): \(String(cString: strerror(errno)))") }
}
