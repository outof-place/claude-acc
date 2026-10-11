// The client side of the event stream (EventHub), for `acc-cored events` and the tests: connect,
// ask for events, subscribe, then read frames. Hosts (AccKit) speak the same frames.
import Darwin

public enum EventClient {
    public struct Frame: Equatable, Sendable {
        public var kind: UInt8
        /// hello, gone and error: the JSON payload; snapshot: its header
        public var header: PyObject
        /// snapshot: the file's bytes
        public var data: [UInt8]
    }

    /// A connected descriptor that asked for events, nil if the daemon isn't there.
    public static func connect(_ path: String) -> Int32? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd)
            return nil
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        guard ok == 0, send(fd, Array("{\"op\":\"events\",\"v\":1}\n".utf8)) else {
            close(fd)
            return nil
        }
        return fd
    }

    public static func subscribe(_ fd: Int32, topics: [String], have: [String: String] = [:], maxHz: [String: Double] = [:]) -> Bool {
        let payload = PyJSON.object(PyObject([
            ("topics", .array(topics.map { .string($0) })),
            ("have", .object(PyObject(have.sorted { $0.key < $1.key }.map { ($0.key, .string($0.value)) }))),
            ("maxHz", .object(PyObject(maxHz.sorted { $0.key < $1.key }.map { ($0.key, .double($0.value)) }))),
        ])).dumps()
        return send(fd, EventHub.frame(2, Array(payload.utf8)))
    }

    static func send(_ fd: Int32, _ bytes: [UInt8]) -> Bool {
        var sent = 0
        while sent < bytes.count {
            let w = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress! + sent, $0.count - sent) }
            if w < 0 && errno == EINTR { continue }
            if w <= 0 { return false }
            sent += w
        }
        return true
    }

    /// The next frame within `timeout` seconds; nil on a timeout, a close or a malformed frame.
    public static func next(_ fd: Int32, buffer: inout [UInt8], timeout: Double) -> Frame? {
        let deadline = Kernel.monotonic() + timeout
        while true {
            if buffer.count >= 5 {
                let n = Int(buffer[0]) | Int(buffer[1]) << 8 | Int(buffer[2]) << 16 | Int(buffer[3]) << 24
                if n >= 1, buffer.count >= 4 + n {
                    let kind = buffer[4]
                    let payload = Array(buffer[5..<(4 + n)])
                    buffer.removeFirst(4 + n)
                    if kind == 3 {
                        guard payload.count >= 2 else { return nil }
                        let h = Int(payload[0]) | Int(payload[1]) << 8
                        guard payload.count >= 2 + h, let header = (try? PyJSON.loads(bytes: Array(payload[2..<(2 + h)])))?.object else { return nil }
                        return Frame(kind: kind, header: header, data: Array(payload[(2 + h)...]))
                    }
                    return Frame(kind: kind, header: (try? PyJSON.loads(bytes: payload))?.object ?? PyObject(), data: [])
                }
            }
            let left = deadline - Kernel.monotonic()
            if left <= 0 { return nil }
            var p = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let r = poll(&p, 1, Int32(max(1, left * 1000)))
            if r < 0 && errno == EINTR { continue }
            if r <= 0 { return nil }
            var chunk = [UInt8](repeating: 0, count: 64 * 1024)
            let got = chunk.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if got < 0 && errno == EINTR { continue }
            if got <= 0 { return nil }
            buffer += chunk[..<got]
        }
    }

    /// `acc-cored events`: one line per frame until the daemon closes or `seconds` pass.
    public static func watch(socket path: String, topics: [String], seconds: Double?) {
        guard let fd = connect(path) else {
            print("acc-cored: brak demona na \(path)")
            return
        }
        defer { close(fd) }
        guard subscribe(fd, topics: topics.isEmpty ? EventHub.topics : topics) else { return }
        let end = seconds.map { Kernel.monotonic() + $0 }
        var buffer: [UInt8] = []
        while end.map({ Kernel.monotonic() < $0 }) ?? true {
            guard let f = next(fd, buffer: &buffer, timeout: end.map { $0 - Kernel.monotonic() } ?? 3600) else {
                if end == nil { continue }
                return
            }
            let names = [1: "hello", 3: "snapshot", 4: "gone", 5: "error"]
            let what = f.kind == 3
                ? "\(f.header["topic"]?.string ?? "?") \(f.header["seq"]?.string ?? "?") \(f.data.count) B"
                : PyJSON.object(f.header).dumps()
            print("\(Int(Kernel.wall())) \(names[Int(f.kind)] ?? "kind \(f.kind)") \(what)")
        }
    }
}
