import Darwin
import Dispatch
import Foundation
import Testing
@testable import AccCore

// The caps' gate: when `devguard caps` could do something new.

private func capsHome(_ name: String, caps: String) throws -> (home: String, watch: CapsWatch, queue: DispatchQueue) {
    var template = Array("/tmp/acc-cored-caps-\(name)-XXXXXX".utf8CString)
    let home = try #require(template.withUnsafeMutableBufferPointer { mkdtemp($0.baseAddress) }.map { String(cString: $0) })
    Files.mkdirs(GuardPaths.state(home))
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/janitor.json", #"{"caps": \#(caps.replacingOccurrences(of: "HOME", with: home))}"#))
    let queue = DispatchQueue(label: "caps-test-\(name)")
    return (home, CapsWatch(home: home, queue: queue), queue)
}

/// Waits up to 5 s for the stream to mark the watch dirty (FSEvents latency is 1 s).
private func becomesDirty(_ watch: CapsWatch, _ queue: DispatchQueue) -> Bool {
    for _ in 0..<50 {
        if queue.sync(execute: { watch.dirty }) { return true }
        usleep(100_000)
    }
    return false
}

@Test("the static part of a cap's glob, and the forms only expanduser or the working directory can place")
func capPrefixes() {
    #expect(CapsWatch.prefix("~/.cache/perf/*/builds", home: "/h") == "/h/.cache/perf")
    #expect(CapsWatch.prefix("~", home: "/h") == "/h")
    #expect(CapsWatch.prefix("/x/y/builds/", home: "/h") == "/x/y/builds")
    #expect(CapsWatch.prefix("/x/y[0-9]/z", home: "/h") == "/x")
    #expect(CapsWatch.prefix("~user/.cache/q/*", home: "/h") == nil)
    #expect(CapsWatch.prefix("$HOME/.cache/q/*", home: "/h") == nil)
    #expect(CapsWatch.prefix("rel/*/builds", home: "/h") == nil)
}

@Test("the gate: a change, busy entries, a nil report, retry_at, maxGap and a janitor.json change run it; nothing else does")
func capsGate() throws {
    let (home, watch, queue) = try capsHome("gate", caps: #"[{"path": "HOME/caps/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/a/builds")
    let now = Kernel.wall()
    queue.sync {
        #expect(watch.due(now: now))  // first interval: nothing known yet
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0), ("retry_at", .null)]))
        #expect(!watch.due(now: now + 600))
        watch.finished(nil)  // a run that said nothing counts as busy
        #expect(watch.due(now: now + 600))
        watch.finished(PyObject([("busy", 2)]))
        #expect(watch.due(now: now + 600))
        watch.finished(PyObject([("busy", 0), ("retry_at", .double(now + 900))]))
        #expect(!watch.due(now: now + 600))
        #expect(watch.due(now: now + 900))
        watch.finished(PyObject([("busy", 0)]))
        #expect(watch.due(now: now + 6 * 3600))  // maxGap
    }
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/janitor.json", #"{"caps": [{"path": "\#(home)/caps/*/out"}]}"#))
    queue.sync { #expect(watch.due(now: now + 600)) }
    queue.sync { watch.stop() }
}

@Test("events: a write under a capped folder, one during a run, and a root replaced by a rename")
func capsEvents() throws {
    let (home, watch, queue) = try capsHome("events", caps: #"[{"path": "HOME/caps/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    let builds = home + "/caps/builds"
    Files.mkdirs(builds)
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))
    }
    #expect(!becomesDirty(watch, queue))
    #expect(Files.writeAtomic(builds + "/one.bin", "x"))
    #expect(becomesDirty(watch, queue))
    queue.sync { watch.started(now: now) }  // a run starts; a write during it counts for the next
    #expect(Files.writeAtomic(builds + "/two.bin", "x"))
    #expect(becomesDirty(watch, queue))

    // `mv stage builds`: the event goes to the parent, which only WatchRoot reports
    queue.sync {
        _ = watch.due(now: now + 600)
        watch.started(now: now + 600)
    }
    #expect(rename(builds, home + "/caps/old") == 0)
    Files.mkdirs(home + "/stage")
    #expect(rename(home + "/stage", builds) == 0)
    #expect(becomesDirty(watch, queue))
    queue.sync {
        #expect(watch.due(now: now + 1200))  // re-armed on the new folder
        watch.started(now: now + 1200)
    }
    #expect(Files.writeAtomic(builds + "/three.bin", "x"))
    #expect(becomesDirty(watch, queue))
    queue.sync { watch.stop() }
}

@Test("a folder the last run matched through a link is watched where it lives")
func capsLinkedFolder() throws {
    let (home, watch, queue) = try capsHome("link", caps: #"[{"path": "HOME/caps/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    let target = home + "/elsewhere/builds"
    Files.mkdirs(target)
    Files.mkdirs(home + "/caps/wt")
    #expect(symlink(target, home + "/caps/wt/builds") == 0)
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0), ("folders", .array([.string(Files.realpath(target))]))]))
        #expect(watch.due(now: now + 600))  // the new folder re-arms the stream
        watch.started(now: now + 600)
        #expect(watch.prefixes.contains(Files.realpath(target)))
    }
    #expect(Files.writeAtomic(target + "/out.bin", "x"))
    #expect(becomesDirty(watch, queue))
    queue.sync { watch.stop() }
}

@Test("a file growing behind an open descriptor shows up as lost free space")
func capsGrowingFile() throws {
    // max_gb 1e-5: the threshold is a tenth of it (~1 KiB), so 64 MiB is far above the noise
    let (home, watch, queue) = try capsHome("grow", caps: #"[{"path": "HOME/caps/*", "max_gb": 0.00001}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/rec")
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))
    }
    let fd = open(home + "/caps/rec/take.mov", O_WRONLY | O_CREAT, 0o600)
    defer { close(fd) }
    let chunk = [UInt8](repeating: 7, count: 1 << 20)
    for _ in 0..<64 { _ = chunk.withUnsafeBytes { write(fd, $0.baseAddress, $0.count) } }
    fsync(fd)
    queue.sync { #expect(watch.due(now: now + 600)) }
    queue.sync { watch.stop() }
}

@Test("a cap this can't place, and no caps at all")
func capsUnplaced() throws {
    let (home, watch, queue) = try capsHome("unplaced", caps: #"[{"path": "~nobody/.cache/q/*"}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))
        #expect(watch.due(now: now + 600))  // every interval, as in Python
    }
    #expect(Files.writeAtomic(GuardPaths.state(home) + "/janitor.json", #"{"caps": []}"#))
    queue.sync { #expect(!watch.due(now: now + 1200)) }
}

@Test("a capped folder made later, in another letter case, re-arms the watch at the next interval")
func capsFolderMadeLater() throws {
    let (home, watch, queue) = try capsHome("later", caps: #"[{"path": "HOME/Caps/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))
        #expect(!watch.due(now: now + 600))
    }
    Files.mkdirs(home + "/caps/a/builds")  // APFS is case-insensitive: the same folder
    queue.sync {
        #expect(watch.due(now: now + 1200))
        #expect(watch.prefixes == [Files.realpath(home) + "/caps"])
        watch.stop()
    }
}
