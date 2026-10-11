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

    init(_ name: String, _ configure: (EventHub) -> Void = { _ in }) throws {
        var template = Array("/tmp/acc-cored-ev-\(name)-XXXXXX".utf8CString)
        dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
        Files.mkdirs(dir + "/sched")
        hub = EventHub(stateDir: dir)
        configure(hub)
        fd = Self.client(hub)
    }

    /// One more client on the hub: the test's end of a socket pair.
    static func client(_ hub: EventHub) -> Int32 {
        var fds: [Int32] = [0, 0]
        #expect(socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0)
        hub.adopt(fds[0], leftover: [])
        return fds[1]
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

/// A ping that waits out a full listen backlog (refused at connect under a burst): false only for a
/// connection that got no answer.
private func ping(_ path: String) -> Bool {
    for _ in 0..<50 {
        if let answer = UnixLineClient.exchange(path: path, line: #"{"op":"ping"}"#, timeout: 2, stop: { try? PyJSON.loads($0) }) {
            return answer["ok"]?.truthy == true
        }
        usleep(10_000)
    }
    return false
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

@Test("a folder missing at subscribe, made later, moved away and made again: its topic follows")
func eventSubfolders() throws {
    let p = try Pair("dirs")
    try FileManager.default.removeItem(atPath: p.dir + "/sched")
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["sched"]))
    #expect(p.next()?.kind == 4)
    Files.mkdirs(p.dir + "/sched")
    p.write("sched/state.json", #"{"n": 1}"#)
    #expect(p.drain(1).last?.data == Array(#"{"n": 1}"#.utf8))
    // moved away (gone), made again, written
    #expect(rename(p.dir + "/sched", p.dir + "/sched.old") == 0)
    #expect(p.next()?.kind == 4)
    Files.mkdirs(p.dir + "/sched")
    p.write("sched/state.json", #"{"n": 2}"#)
    #expect(p.drain(1).last?.data == Array(#"{"n": 2}"#.utf8))
    // a write in place to the new folder's file, and not to the moved one's
    let fd = open(p.dir + "/sched/state.json", O_WRONLY | O_TRUNC)
    _ = Array(#"{"n": 3}"#.utf8).withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
    close(fd)
    #expect(p.drain(1).last?.data == Array(#"{"n": 3}"#.utf8))
    #expect(Files.writeAtomic(p.dir + "/sched.old/state.json", #"{"old": true}"#))
    #expect(p.drain(0.5).isEmpty)
    // a new subscribe holding nothing gets what is there now
    #expect(EventClient.subscribe(p.fd, topics: ["sched"]))
    #expect(p.next()?.data == Array(#"{"n": 3}"#.utf8))
}

@Test("maxHz: a subnormal rate is clamped (it recursed until SIGBUS); a slow topic doesn't hold back a fast one")
func eventHzEdges() throws {
    let p = try Pair("hzedge")
    for name in ["owner.json", "devguard-state.json", "sched/state.json"] { p.write(name, #"{"n": 0}"#) }
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["owner"], maxHz: ["owner": 1e-320]))
    #expect(p.next()?.data == Array(#"{"n": 0}"#.utf8))
    p.write("owner.json", #"{"n": 1}"#)
    p.write("owner.json", #"{"n": 2}"#)
    #expect(p.drain(0.5).isEmpty)  // the next owner frame waits 1000 s
    #expect(p.hub.count == 1)

    #expect(EventClient.subscribe(p.fd, topics: ["devguard", "sched"], maxHz: ["devguard": 0.2, "sched": 5]))
    #expect(p.drain(0.5).count == 2)
    p.write("sched/state.json", #"{"n": 1}"#)
    #expect(p.next()?.data == Array(#"{"n": 1}"#.utf8))
    p.write("devguard-state.json", #"{"n": 1}"#)  // its turn in ~4.5 s
    usleep(30_000)
    p.write("sched/state.json", #"{"n": 2}"#)  // its turn in under 0.2 s
    let start = Kernel.monotonic()
    let next = p.next(3)
    #expect(next?.header["topic"]?.string == "sched" && next?.data == Array(#"{"n": 2}"#.utf8))
    #expect(Kernel.monotonic() - start < 1)
}

@Test("a FIFO or a link in place of a state file is gone, and doesn't hold the hub")
func eventOddFiles() throws {
    let p = try Pair("odd")
    #expect(mkfifo(p.dir + "/janitor-state.json", 0o600) == 0)
    p.write("elsewhere.json", "{}")
    #expect(symlink(p.dir + "/elsewhere.json", p.dir + "/owner.json") == 0)
    p.write("status.json", "{}")
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["janitor", "owner", "accounts"]))
    let frames = p.drain(0.5)
    #expect(Set(frames.filter { $0.kind == 4 }.compactMap { $0.header["topic"]?.string }) == ["janitor", "owner"])
    #expect(frames.contains { $0.kind == 3 && $0.header["topic"]?.string == "accounts" })
    let start = Kernel.monotonic()
    #expect(p.hub.count == 1)
    #expect(Kernel.monotonic() - start < 0.1)
}

@Test("past maxClients a client gets busy; one that never subscribes or stops reading is dropped")
func eventLimits() throws {
    let p = try Pair("limits") { hub in
        hub.maxClients = 2
        hub.subscribeSeconds = 0.4
        hub.stallSeconds = 0.4
        hub.reapEvery = 0.1
    }
    p.write("status.json", "{\"pad\": \"" + String(repeating: "x", count: 600_000) + "\"}")
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["accounts"]))  // and then never reads
    let silent = Pair.client(p.hub)
    defer { close(silent) }
    let third = Pair.client(p.hub)
    defer { close(third) }
    var buffer: [UInt8] = []
    let busy = EventClient.next(third, buffer: &buffer, timeout: 2)
    #expect(busy?.kind == 5 && busy?.header["code"]?.string == "busy")
    #expect(EventClient.read(third, buffer: &buffer, timeout: 2) == .closed)
    for _ in 0..<40 where p.hub.count > 0 { usleep(50_000) }
    #expect(p.hub.count == 0)  // the silent one after 0.4 s, the stalled one after 0.4 s of a full socket
}

@Test("a slow reader that keeps taking stays past stallSeconds and gets the whole frame")
func eventSlowReader() throws {
    let p = try Pair("slow") { hub in
        hub.stallSeconds = 0.5
        hub.reapEvery = 0.1
    }
    let body = "{\"pad\": \"" + String(repeating: "y", count: 300_000) + "\"}"
    p.write("status.json", body)
    _ = p.next()
    #expect(EventClient.subscribe(p.fd, topics: ["accounts"]))
    // one read every 0.1 s, of what a socket pair's ~8 KB buffer holds: seconds for the frame,
    // several stall periods
    var got: [UInt8] = []
    var chunk = [UInt8](repeating: 0, count: 64 * 1024)
    let start = Kernel.monotonic()
    while got.count < body.utf8.count + 64, Kernel.monotonic() - start < 20 {
        usleep(100_000)
        let n = chunk.withUnsafeMutableBytes { Darwin.read(p.fd, $0.baseAddress, $0.count) }
        if n <= 0 { break }
        got += chunk[..<n]
    }
    #expect(Kernel.monotonic() - start > 1)
    #expect(got.count >= body.utf8.count)
    #expect(p.hub.count == 1)
}

@Test("a client gone before its hello, and a stream the daemon closes, end without SIGPIPE or a spin")
func eventPeerGone() throws {
    let p = try Pair("gone")
    // swift test doesn't ignore SIGPIPE: one raised here ends the test run
    for _ in 0..<200 {
        var fds: [Int32] = [0, 0]
        #expect(socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0)
        close(fds[1])
        p.hub.adopt(fds[0], leftover: [])
    }
    _ = p.next()
    for _ in 0..<40 where p.hub.count > 1 { usleep(50_000) }
    #expect(p.hub.count == 1)
    #expect(EventClient.subscribe(p.fd, topics: ["owner"]))
    _ = p.drain(0.3)
    p.hub.stop()
    #expect(EventClient.read(p.fd, buffer: &p.buffer, timeout: 2) == .closed)
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

    // handovers that end at once (a bad frame, a client gone) among pings: every ping answered,
    // no descriptor closed under another connection
    for i in 0..<300 {
        if i % 3 == 0 {
            #expect(ping(dir + "/s"))
            continue
        }
        guard let c = EventClient.connect(dir + "/s") else { continue }
        if i % 3 == 1 { _ = EventClient.send(c, EventHub.frame(9, [1])) }
        close(c)
    }
    #expect(EventClient.subscribe(fd, topics: ["owner"], have: [:]))
    #expect(EventClient.next(fd, buffer: &buffer, timeout: 3)?.data == Array(#"{"owner": "pod"}"#.utf8))  // held nothing: sent again
    #expect(Files.writeAtomic(dir + "/owner.json", #"{"owner": "orca"}"#))
    #expect(EventClient.next(fd, buffer: &buffer, timeout: 3)?.data == Array(#"{"owner": "orca"}"#.utf8))
    for _ in 0..<40 where hub.count > 1 { usleep(50_000) }
    #expect(hub.count == 1)
}

@Test("acc-cored events without --for ends when the daemon closes the stream")
func eventWatchEnds() throws {
    var template = Array("/tmp/acc-cored-evw-XXXXXX".utf8CString)
    let dir = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let hub = EventHub(stateDir: dir)
    let control = ControlSocket(path: dir + "/s") { _ in .object(PyObject([("ok", true)])) }
    control.upgrade = { fd, rest in hub.adopt(fd, leftover: rest) }
    #expect(control.start())
    let done = DispatchSemaphore(value: 0)
    Thread.detachNewThread {
        EventClient.watch(socket: dir + "/s", topics: ["owner"], seconds: nil)
        done.signal()
    }
    for _ in 0..<40 where hub.count == 0 { usleep(50_000) }
    #expect(hub.count == 1)
    control.stop()
    hub.stop()
    #expect(done.wait(timeout: .now() + 3) == .success)
}
