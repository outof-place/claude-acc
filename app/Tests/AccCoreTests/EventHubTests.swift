import Darwin
import Foundation
import Testing
@testable import AccCore

// The event stream: snapshots of the state files, latest-wins, maxHz, have, gone, errors.

private final class Pair {
    let hub: EventHub
    let fd: Int32
    var buffer: [UInt8] = []
    let dir: String

    init(_ name: String) throws {
        var template = Array("/tmp/acc-cored-ev-\(name)-XXXXXX".utf8CString)
        dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
        Files.mkdirs(dir + "/sched")
        hub = EventHub(stateDir: dir)
        var fds: [Int32] = [0, 0]
        #expect(socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0)
        fd = fds[1]
        hub.adopt(fds[0], leftover: [])
    }

    func next(_ timeout: Double = 3) -> EventClient.Frame? { EventClient.next(fd, buffer: &buffer, timeout: timeout) }

    /// Frames until `seconds` pass with none.
    func drain(_ seconds: Double) -> [EventClient.Frame] {
        var out: [EventClient.Frame] = []
        while let f = next(seconds) { out.append(f) }
        return out
    }

    func write(_ name: String, _ text: String) { #expect(Files.writeAtomic(dir + "/" + name, text)) }

    deinit {
        hub.stop()
        close(fd)
        try? FileManager.default.removeItem(atPath: dir)
    }
}

@Test("hello, then a snapshot per subscribed file with its exact bytes and stamp, gone for a missing one")
func eventSnapshots() throws {
    let p = try Pair("snap")
    p.write("status.json", #"{"generated_at": 1.5, "accounts": []}"#)
    let hello = try #require(p.next())
    #expect(hello.kind == 1 && hello.header["v"]?.int == 1)
    #expect(hello.header["topics"]?.array?.contains("accountsSlot") == true)

    #expect(EventClient.subscribe(p.fd, topics: ["accounts", "janitor"]))
    let frames = p.drain(0.5)
    let snap = try #require(frames.first { $0.kind == 3 })
    #expect(snap.header["topic"]?.string == "accounts")
    #expect(snap.data == Array(#"{"generated_at": 1.5, "accounts": []}"#.utf8))
    var st = stat()
    stat(p.dir + "/status.json", &st)
    let ns = Int64(st.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(st.st_mtimespec.tv_nsec)
    #expect(snap.header["seq"]?.string == "\(st.st_ino):\(ns):\(st.st_size)")
    #expect(frames.contains { $0.kind == 4 && $0.header["topic"]?.string == "janitor" })

    // a rename, a write in place, a removal, a new file
    p.write("status.json", #"{"generated_at": 2.5}"#)
    #expect(p.next()?.data == Array(#"{"generated_at": 2.5}"#.utf8))
    let fd = open(p.dir + "/status.json", O_WRONLY | O_TRUNC)
    _ = Array(#"{"generated_at": 3.5}"#.utf8).withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
    close(fd)
    #expect(p.drain(0.5).last?.data == Array(#"{"generated_at": 3.5}"#.utf8))
    unlink(p.dir + "/status.json")
    #expect(p.next()?.kind == 4)
    p.write("janitor-state.json", #"{"sweeps": []}"#)
    #expect(p.next()?.header["topic"]?.string == "janitor")
}

@Test("latest-wins under maxHz: a burst of writes gives few frames, the last one current")
func eventMaxHz() throws {
    let p = try Pair("hz")
    p.write("sched/state.json", #"{"n": 0}"#)
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["sched"], maxHz: ["sched": 2]))
    #expect(p.next()?.data == Array(#"{"n": 0}"#.utf8))
    for i in 1...10 {
        p.write("sched/state.json", "{\"n\": \(i)}")
        usleep(20_000)
    }
    let frames = p.drain(1.2)
    #expect(frames.count >= 1 && frames.count <= 3)
    #expect(frames.last?.data == Array(#"{"n": 10}"#.utf8))
}

@Test("have: what the client holds isn't sent again; pressure is sent only when the level changes")
func eventHaveAndPressure() throws {
    let p = try Pair("have")
    p.write("devguard-state.json", #"{"snapshot": {"pressure": {"level": 1}}, "big": "x"}"#)
    p.write("owner.json", #"{"owner": "pod"}"#)
    _ = p.next()
    let seq = EventHub.read(p.dir + "/owner.json").seq
    #expect(EventClient.subscribe(p.fd, topics: ["owner", "pressure"], have: ["owner": seq]))
    let first = p.drain(0.5)
    #expect(first.count == 1 && first[0].header["topic"]?.string == "pressure" && first[0].data == Array(#"{"level":1}"#.utf8))
    p.write("devguard-state.json", #"{"snapshot": {"pressure": {"level": 1}}, "big": "y"}"#)
    #expect(p.drain(0.5).isEmpty)  // the same level: nothing
    p.write("devguard-state.json", #"{"snapshot": {"pressure": {"level": 2}}}"#)
    #expect(p.next()?.data == Array(#"{"level":2}"#.utf8))
    // a new set without owner, then owner again with nothing held: sent again
    #expect(EventClient.subscribe(p.fd, topics: ["pressure"], have: ["pressure": "x"]))
    _ = p.drain(0.3)
    #expect(EventClient.subscribe(p.fd, topics: ["owner"]))
    #expect(p.next()?.header["topic"]?.string == "owner")
}

@Test("an unknown topic is an error and the rest still flows; a frame that isn't a subscribe closes the stream")
func eventErrors() throws {
    let p = try Pair("err")
    p.write("owner.json", "{}")
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["owner", "nope"]))
    let frames = p.drain(0.5)
    #expect(frames.contains { $0.kind == 5 && $0.header["code"]?.string == "topic" })
    #expect(frames.contains { $0.kind == 3 })
    #expect(EventClient.send(p.fd, EventHub.frame(9, [1, 2, 3])))
    #expect(p.next()?.kind == 5)
    #expect(p.next(0.5) == nil)  // closed
    #expect(p.hub.count == 0)
}

@Test("through the control socket: the events line upgrades the connection, ping still answers")
func eventUpgrade() throws {
    var template = Array("/tmp/acc-cored-evs-XXXXXX".utf8CString)
    let dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: dir) }
    #expect(Files.writeAtomic(dir + "/owner.json", #"{"owner": "pod"}"#))
    let hub = EventHub(stateDir: dir)
    let control = ControlSocket(path: dir + "/s") { _ in .object(PyObject([("ok", true)])) }
    control.upgrade = { fd, rest in hub.adopt(fd, leftover: rest) }
    #expect(control.start())
    defer {
        control.stop()
        hub.stop()
    }
    let fd = try #require(EventClient.connect(dir + "/s"))
    defer { close(fd) }
    var buffer: [UInt8] = []
    #expect(EventClient.next(fd, buffer: &buffer, timeout: 3)?.kind == 1)
    #expect(EventClient.subscribe(fd, topics: ["owner"]))
    #expect(EventClient.next(fd, buffer: &buffer, timeout: 3)?.data == Array(#"{"owner": "pod"}"#.utf8))
    #expect(UnixLineClient.exchange(path: dir + "/s", line: #"{"op":"ping"}"#, timeout: 2) { try? PyJSON.loads($0) }?["ok"]?.truthy == true)
}
