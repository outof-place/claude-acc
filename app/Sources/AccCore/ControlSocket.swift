// acc-cored's control socket ($STATE/acc-cored.sock): one JSON object per line each way. The
// socket is 0600 in the user's state directory and every peer's uid is checked (LOCAL_PEERCRED).
// Requests are answered on the socket's own queue; a handler that needs an engine's queue syncs.
import Darwin
import Dispatch

public final class ControlSocket: @unchecked Sendable {
    public typealias Handler = @Sendable (PyObject) -> PyJSON

    let path: String
    let handler: Handler
    let queue = DispatchQueue(label: "acc-cored.control", qos: .userInitiated)
    var listener: DispatchSourceRead?
    var clients: [Int32: (DispatchSourceRead, [UInt8])] = [:]

    public init(path: String, handler: @escaping Handler) {
        self.path = path
        self.handler = handler
    }

    /// Binds (replacing a stale socket file) and starts accepting; false when it can't.
    public func start() -> Bool {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd)
            return false
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        unlink(path)
        let old = umask(0o077)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        umask(old)
        guard bound == 0, chmod(path, 0o600) == 0, listen(fd, 16) == 0 else {
            close(fd)
            return false
        }
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setEventHandler { [weak self] in self?.accept(fd) }
        source.setCancelHandler { close(fd) }
        source.resume()
        listener = source
        return true
    }

    public func stop() {
        queue.sync {
            listener?.cancel()
            for (_, (source, _)) in clients { source.cancel() }
            clients.removeAll()
        }
        unlink(path)
    }

    func accept(_ fd: Int32) {
        while true {
            let client = Darwin.accept(fd, nil, nil)
            guard client >= 0 else { return }
            var cred = xucred()
            var len = socklen_t(MemoryLayout<xucred>.size)
            guard getsockopt(client, SOL_LOCAL, LOCAL_PEERCRED, &cred, &len) == 0, cred.cr_uid == getuid() else {
                close(client)
                continue
            }
            var on: Int32 = 1
            setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
            _ = fcntl(client, F_SETFL, fcntl(client, F_GETFL) | O_NONBLOCK)
            let source = DispatchSource.makeReadSource(fileDescriptor: client, queue: queue)
            source.setEventHandler { [weak self] in self?.read(client) }
            source.setCancelHandler { close(client) }
            clients[client] = (source, [])
            source.resume()
        }
    }

    func read(_ fd: Int32) {
        guard var entry = clients[fd] else { return }
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        let n = chunk.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
        if n <= 0 {
            if n < 0 && (errno == EAGAIN || errno == EINTR) { return }
            entry.0.cancel()
            clients[fd] = nil
            return
        }
        entry.1 += chunk[..<n]
        // one request may come in pieces; a line over 1 MiB is not ours
        if entry.1.count > 1 << 20 {
            entry.0.cancel()
            clients[fd] = nil
            return
        }
        while let nl = entry.1.firstIndex(of: 0x0A) {
            let line = Array(entry.1[..<nl])
            entry.1.removeSubrange(...nl)
            let answer: PyJSON
            if let request = (try? PyJSON.loads(bytes: line))?.object {
                answer = handler(request)
            } else {
                answer = .object(PyObject([("ok", false), ("error", "not a JSON object")]))
            }
            let out = Array((answer.dumps() + "\n").utf8)
            var sent = 0
            while sent < out.count {
                let w = out.withUnsafeBytes { Darwin.write(fd, $0.baseAddress! + sent, $0.count - sent) }
                if w <= 0 {
                    if w < 0 && errno == EINTR { continue }
                    if w < 0 && errno == EAGAIN { usleep(1000); continue }
                    break
                }
                sent += w
            }
        }
        clients[fd] = entry
    }
}
