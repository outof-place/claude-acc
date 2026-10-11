import Darwin
import Foundation
import Testing
@testable import AccCore

// acc-cored's control socket: answers, and the limits that keep a bad client from wedging it.

private func socketDir() throws -> String {
    var template = Array("/tmp/acs-sock-XXXXXX".utf8CString)
    return try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
}

private func connect(_ path: String) -> Int32 {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: bytes)
        raw[bytes.count] = 0
    }
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    if ok != 0 {
        close(fd)
        return -1
    }
    return fd
}

@Test("one JSON line in, one out; the socket file is 0600; a path over sun_path is refused")
func controlAnswers() throws {
    let dir = try socketDir()
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let socket = ControlSocket(path: dir + "/c.sock") { request in .object(PyObject([("ok", true), ("echo", request["op"] ?? .null)])) }
    #expect(socket.start())
    defer { socket.stop() }
    let answer = UnixLineClient.exchange(path: dir + "/c.sock", line: #"{"op": "ping"}"#, timeout: 2) { try? PyJSON.loads($0) }
    #expect(answer?["echo"] == .string("ping"))
    var st = stat()
    #expect(stat(dir + "/c.sock", &st) == 0 && st.st_mode & 0o777 == 0o600)
    let bad = UnixLineClient.exchange(path: dir + "/c.sock", line: "nope", timeout: 2) { try? PyJSON.loads($0) }
    #expect(bad?["ok"] == .bool(false))
    #expect(!ControlSocket(path: dir + "/" + String(repeating: "x", count: 120)) { _ in .null }.start())
}

@Test("clients that close before their answer (some before accept) never raise SIGPIPE")
func controlPeerGone() throws {
    let dir = try socketDir()
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let socket = ControlSocket(path: dir + "/c.sock") { _ in .object(PyObject([("ok", true)])) }
    #expect(socket.start())
    defer { socket.stop() }
    // swift test doesn't ignore SIGPIPE: one raised here ends the whole test run (exit 141)
    for _ in 0..<400 {
        let fd = connect(dir + "/c.sock")
        guard fd >= 0 else { continue }
        _ = Array(#"{"op": "ping"}"#.utf8 + [0x0A]).withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
        close(fd)
    }
    // under a full suite the burst can still fill the backlog or maxClients: a refused ping is tried again
    var ok: PyJSON?
    for _ in 0..<50 where ok == nil {
        ok = UnixLineClient.exchange(path: dir + "/c.sock", line: #"{"op": "ping"}"#, timeout: 3) { try? PyJSON.loads($0) }
        if ok == nil { usleep(20_000) }
    }
    #expect(ok?["ok"] == .bool(true))
}

@Test("a client that doesn't read its answer is dropped after writeSeconds; others still get answers")
func controlWriteDeadline() throws {
    let dir = try socketDir()
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let big = String(repeating: "x", count: 4 << 20)
    let socket = ControlSocket(path: dir + "/c.sock") { request in
        request["op"] == .string("big") ? .string(big) : .object(PyObject([("ok", true)]))
    }
    socket.writeSeconds = 0.3
    #expect(socket.start())
    defer { socket.stop() }
    let lazy = connect(dir + "/c.sock")
    #expect(lazy >= 0)
    defer { close(lazy) }
    _ = Array(#"{"op": "big"}"#.utf8 + [0x0A]).withUnsafeBytes { Darwin.write(lazy, $0.baseAddress, $0.count) }
    let start = Kernel.wall()
    let ok = UnixLineClient.exchange(path: dir + "/c.sock", line: #"{"op": "ping"}"#, timeout: 3) { try? PyJSON.loads($0) }
    #expect(ok?["ok"] == .bool(true))
    #expect(Kernel.wall() - start < 2)
}

@Test("past maxClients a connection is closed at once; silent clients go after idleSeconds")
func controlLimits() throws {
    let dir = try socketDir()
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let socket = ControlSocket(path: dir + "/c.sock") { _ in .object(PyObject([("ok", true)])) }
    socket.maxClients = 2
    socket.idleSeconds = 0.5
    #expect(socket.start())
    defer { socket.stop() }
    let a = connect(dir + "/c.sock"), b = connect(dir + "/c.sock")
    defer { close(a); close(b) }
    usleep(200_000)
    let third = connect(dir + "/c.sock")
    defer { close(third) }
    var byte: UInt8 = 0
    usleep(200_000)
    #expect(Darwin.read(third, &byte, 1) == 0)  // closed by the server
    #expect(socket.queue.sync { socket.clients.count } == 2)
    for _ in 0..<80 where socket.queue.sync(execute: { socket.clients.count }) > 0 { usleep(100_000) }
    #expect(socket.queue.sync { socket.clients.count } == 0)
}
