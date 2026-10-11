// acc-cored's control socket ($STATE/acc-cored.sock): one JSON object per line each way. The
// socket is 0600 in the user's state directory and every peer's uid is checked (LOCAL_PEERCRED).
// Requests are answered on the socket's own queue; a handler that needs an engine's queue syncs.
// A client gets `maxClients` at most, `idleSeconds` without a line before it is closed, and
// `writeSeconds` to take an answer; with no descriptor left (EMFILE) the listener rests a second.
// The line {"op": "events"} hands the connection to `upgrade` (EventHub), with what followed it.
import Darwin
import Dispatch

public final class ControlSocket: @unchecked Sendable {
    public typealias Handler = @Sendable (PyObject) -> PyJSON

    let path: String
    let handler: Handler
    let queue = DispatchQueue(label: "acc-cored.control", qos: .userInitiated)
    var listener: DispatchSourceRead?
    var clients: [Int32: (DispatchSourceRead, [UInt8])] = [:]
    var lastSeen: [Int32: Double] = [:]
    var reaper: DispatchSourceTimer?
    var resting = false
    public var maxClients = 32
    public var idleSeconds = 30.0
    public var writeSeconds = 1.0
    /// takes a connection that asked for events, and the bytes after its line
    public var upgrade: (@Sendable (Int32, [UInt8]) -> Void)?
    /// connections given to `upgrade`: their read source goes without closing them
    var handedOver: Set<Int32> = []

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
        // a burst of wrappers at once (a build fan-out) fits the backlog; past it connect is refused
        guard bound == 0, chmod(path, 0o600) == 0, listen(fd, SOMAXCONN) == 0 else {
            close(fd)
            return false
        }
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
        let source = DispatchSource.makeReadSource(fileDescriptor: fd, queue: queue)
        source.setEventHandler { [weak self] in self?.accept(fd) }
        source.setCancelHandler { close(fd) }
        source.resume()
        listener = source
        let r = DispatchSource.makeTimerSource(queue: queue)
        r.schedule(deadline: .now() + 5, repeating: 5, leeway: .seconds(1))
        r.setEventHandler { [weak self] in self?.closeIdle() }
        r.resume()
        reaper = r
        return true
    }

    /// Clients silent for `idleSeconds` are closed.
    func closeIdle() {
        let now = Kernel.monotonic()
        for (fd, at) in lastSeen where now - at > idleSeconds { drop(fd) }
    }

    func drop(_ fd: Int32) {
        clients[fd]?.0.cancel()
        clients[fd] = nil
        lastSeen[fd] = nil
    }

    public func stop() {
        queue.sync {
            reaper?.cancel()
            listener?.cancel()
            for (_, (source, _)) in clients { source.cancel() }
            clients.removeAll()
            lastSeen.removeAll()
        }
        unlink(path)
    }

    func accept(_ fd: Int32) {
        while true {
            let client = Darwin.accept(fd, nil, nil)
            guard client >= 0 else {
                // out of descriptors: the connection stays queued and the source would fire at once
                // again; rest the listener a second instead
                if (errno == EMFILE || errno == ENFILE), !resting, let listener {
                    resting = true
                    listener.suspend()
                    queue.asyncAfter(deadline: .now() + 1) { [weak self] in
                        self?.resting = false
                        listener.resume()
                    }
                }
                return
            }
            if clients.count >= maxClients {
                close(client)
                continue
            }
            var cred = xucred()
            var len = socklen_t(MemoryLayout<xucred>.size)
            guard getsockopt(client, SOL_LOCAL, LOCAL_PEERCRED, &cred, &len) == 0, cred.cr_uid == getuid() else {
                close(client)
                continue
            }
            // XNU refuses SO_NOSIGPIPE (EINVAL) on a socket whose peer closed before accept: such a
            // client goes without a write, which would raise SIGPIPE (sends use MSG_NOSIGNAL too)
            var on: Int32 = 1
            guard setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size)) == 0 else {
                close(client)
                continue
            }
            _ = fcntl(client, F_SETFL, fcntl(client, F_GETFL) | O_NONBLOCK)
            let source = DispatchSource.makeReadSource(fileDescriptor: client, queue: queue)
            source.setEventHandler { [weak self] in self?.read(client) }
            source.setCancelHandler { [weak self] in
                if self?.handedOver.remove(client) == nil { close(client) }
            }
            clients[client] = (source, [])
            lastSeen[client] = Kernel.monotonic()
            source.resume()
        }
    }

    /// The whole answer within `writeSeconds` (poll for room on EAGAIN), else false.
    func send(_ out: [UInt8], to fd: Int32) -> Bool {
        let deadline = Kernel.monotonic() + writeSeconds
        var sent = 0
        while sent < out.count {
            let w = out.withUnsafeBytes { Darwin.send(fd, $0.baseAddress! + sent, $0.count - sent, MSG_NOSIGNAL) }
            if w > 0 {
                sent += w
                continue
            }
            if w < 0 && errno == EINTR { continue }
            guard w < 0 && errno == EAGAIN else { return false }
            let left = deadline - Kernel.monotonic()
            if left <= 0 { return false }
            var p = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
            _ = poll(&p, 1, Int32(max(1, left * 1000)))
        }
        return true
    }

    func read(_ fd: Int32) {
        guard var entry = clients[fd] else { return }
        lastSeen[fd] = Kernel.monotonic()
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        let n = chunk.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
        if n <= 0 {
            if n < 0 && (errno == EAGAIN || errno == EINTR) { return }
            drop(fd)
            return
        }
        entry.1 += chunk[..<n]
        // one request may come in pieces; a line over 1 MiB is not ours
        if entry.1.count > 1 << 20 {
            drop(fd)
            return
        }
        while let nl = entry.1.firstIndex(of: 0x0A) {
            let line = Array(entry.1[..<nl])
            entry.1.removeSubrange(...nl)
            let answer: PyJSON
            if let request = (try? PyJSON.loads(bytes: line))?.object, request["op"] == .string("events"), let upgrade {
                // from here on the connection speaks frames, on the event hub's queue
                let rest = entry.1
                handedOver.insert(fd)
                clients[fd]?.0.cancel()
                clients[fd] = nil
                lastSeen[fd] = nil
                upgrade(fd, rest)
                return
            } else if let request = (try? PyJSON.loads(bytes: line))?.object {
                answer = handler(request)
            } else {
                answer = .object(PyObject([("ok", false), ("error", "not a JSON object")]))
            }
            let out = Array((answer.dumps() + "\n").utf8)
            guard send(out, to: fd) else {
                drop(fd)  // a client that doesn't read its answer
                return
            }
        }
        clients[fd] = entry
    }
}
