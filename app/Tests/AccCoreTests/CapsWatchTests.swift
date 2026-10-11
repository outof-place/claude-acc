import CoreServices
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

private final class Volume: @unchecked Sendable { var free: Int64 = 100 << 30 }

@Test("the space gate: only a drop past the tightest folder's headroom, counted from the highest free space since the run")
func capsSpaceGate() throws {
    let (home, watch, queue) = try capsHome("space", caps: #"[{"path": "HOME/caps/*", "max_gb": 1}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/rec")
    let volume = Volume()
    watch.volumes = { _ in ["data": volume.free] }
    let now = Kernel.wall()
    let gb: Int64 = 1 << 30
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0), ("headroom", .double(Double(5 * gb)))]))
        volume.free -= 2 * gb  // a build elsewhere on the volume: within the headroom
        #expect(!watch.due(now: now + 600))
        volume.free -= 4 * gb  // 6 GiB down: a capped file may have outgrown its folder's room
        #expect(watch.due(now: now + 1200))

        watch.started(now: now + 1200)
        watch.finished(PyObject([("busy", 0)]))  // no headroom known: a tenth of the smallest cap
        volume.free += gb * 4 / 10  // the Trash emptied
        #expect(!watch.due(now: now + 1800))
        volume.free -= gb * 3 / 10  // a file grows: 0.3 GiB down from the peak, though above the start
        #expect(watch.due(now: now + 2400))
        watch.stop()
    }
}

@Test("a headroom past Int64 (a cap of 1e10 GB or Infinity) compares as a Double; NaN gives the floor")
func capsHugeHeadroom() throws {
    let (home, watch, queue) = try capsHome("huge", caps: #"[{"path": "HOME/caps/*", "max_gb": 1}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/rec")
    let volume = Volume()
    watch.volumes = { _ in ["data": volume.free] }
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        for (i, headroom) in [1.073741824e19, .infinity, .nan].enumerated() {
            let at = now + Double(i) * 1200
            watch.started(now: at)
            watch.finished(PyObject([("busy", 0), ("headroom", .double(headroom))]))
            volume.free -= 50 << 30
            // a crash here took the guard and sched down at every caps interval
            #expect(watch.due(now: at + 600) == headroom.isNaN)
        }
        watch.stop()
    }
}

@Test("free space sampled between intervals: space freed and growth in one interval don't hide each other")
func capsSampledPeak() throws {
    let (home, watch, queue) = try capsHome("sample", caps: #"[{"path": "HOME/caps/*", "max_gb": 1}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/rec")
    let volume = Volume()
    watch.volumes = { _ in ["data": volume.free] }
    let now = Kernel.wall()
    let gb: Int64 = 1 << 30
    queue.sync {
        _ = watch.due(now: now)
        // freed first, then grown: the peak sampled in between keeps the growth
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))  // the floor: a tenth of 1 GB
        volume.free += gb * 4 / 10
        watch.sample()
        volume.free -= gb * 3 / 10
        #expect(watch.due(now: now + 600))

        // grown first, then freed elsewhere: the sample in between saw the drop
        watch.started(now: now + 600)
        watch.finished(PyObject([("busy", 0)]))
        volume.free -= gb * 3 / 10
        watch.sample()
        volume.free += gb * 4 / 10
        #expect(watch.due(now: now + 1200))

        // without a sample in between, the two cancel out (the interval's own look)
        watch.started(now: now + 1200)
        watch.finished(PyObject([("busy", 0)]))
        volume.free -= gb * 3 / 10
        volume.free += gb * 4 / 10
        #expect(!watch.due(now: now + 1800))
        watch.stop()
    }
}

@Test("a shallow folder (a link right before another wildcard) counts only its own subfolders; deep ones below it count whole")
func capsShallow() throws {
    let (home, watch, queue) = try capsHome("shallow", caps: #"[{"path": "HOME/caps/*/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/far/t1/s0/builds")
    Files.mkdirs(home + "/far/t1/s1/src")
    Files.mkdirs(home + "/caps")
    let t1 = Files.realpath(home + "/far/t1")
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        // s0/builds is there, s1/builds not yet
        watch.finished(PyObject([("busy", 0), ("folders", .array([.string(t1 + "/s0/builds"), .string(t1 + "/s1/builds")])),
                                 ("shallow", .array([.string(t1)]))]))
        #expect(watch.due(now: now + 1))  // new roots: the stream re-arms, and that counts as a change once
        watch.started(now: now + 1)
        #expect(!watch.due(now: now + 600))
        #expect(watch.shallow == [t1])
        watch.event(t1 + "/s1/src", flags: 0)  // an edit in the worktree
        // a file right in a first-level folder, and one in the shallow folder: FSEvents names the
        // folder, but no folder on the way to a capped one came, nor a subfolder
        #expect(Files.writeAtomic(t1 + "/s0/top.swift", "x"))
        watch.event(t1 + "/s0", flags: 0)
        #expect(Files.writeAtomic(t1 + "/s1/top.swift", "x"))
        watch.event(t1 + "/s1", flags: 0)
        #expect(Files.writeAtomic(t1 + "/README", "x"))
        watch.event(t1, flags: 0)
        #expect(!watch.dirty)
        watch.event(t1 + "/s0/builds/run2", flags: 0)  // under a deep folder found in it
        #expect(watch.dirty)
        watch.started(now: now + 600)
        Files.mkdirs(t1 + "/s1/builds")  // a capped folder made on the way
        watch.event(t1 + "/s1", flags: 0)
        #expect(watch.dirty)
        watch.started(now: now + 1200)
        // the run took the folder made before it: a file beside it is an edit again
        #expect(Files.writeAtomic(t1 + "/s1/top2.swift", "x"))
        watch.event(t1 + "/s1", flags: 0)
        #expect(!watch.dirty)
        Files.mkdirs(t1 + "/s2")
        watch.event(t1, flags: 0)  // a new subfolder in it
        #expect(watch.dirty)
        watch.started(now: now + 1800)
        #expect(rename(t1 + "/s2", t1 + "/s3") == 0)  // renamed
        watch.event(t1, flags: 0)
        #expect(watch.dirty)
        watch.stop()
    }
}

@Test("a shallow folder with no deep one found under it yet is a root of its own; a dangling one counts when it's made")
func capsShallowAlone() throws {
    let (home, watch, queue) = try capsHome("alone", caps: #"[{"path": "HOME/caps/*/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/far/t1")
    Files.mkdirs(home + "/caps")
    let far = Files.realpath(home + "/far")
    let t1 = far + "/t1", later = far + "/later2"  // l1 -> far/t1, and l2 -> far/later2, not made yet
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0), ("folders", .array([])), ("shallow", .array([.string(t1), .string(later)]))]))
        #expect(watch.due(now: now + 1))
        watch.started(now: now + 1)
        #expect(watch.shallow == [t1, later])
        #expect(watch.roots.contains(t1))  // no deep folder covers it
        #expect(watch.roots.contains(far))  // the dangling one's nearest folder
        #expect(Files.writeAtomic(t1 + "/README", "x"))
        watch.event(t1, flags: 0)  // a file in it
        #expect(Files.writeAtomic(far + "/notes.txt", "x"))
        watch.event(far, flags: 0)  // a file beside the dangling one
        #expect(!watch.dirty)
        Files.mkdirs(t1 + "/s2")
        watch.event(t1, flags: 0)
        #expect(watch.dirty)
        watch.started(now: now + 600)
        Files.mkdirs(later)
        watch.event(far, flags: 0)  // the dangling one made
        #expect(watch.dirty)
        watch.stop()
    }
}

@Test("above a static prefix not made yet, only a folder on the way counts, not a file written there")
func capsPrefixNotMadeYet() throws {
    let (home, watch, queue) = try capsHome("notyet", caps: #"[{"path": "HOME/cache/perf/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/cache")
    let cache = Files.realpath(home + "/cache")
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        watch.finished(PyObject([("busy", 0)]))
        #expect(!watch.due(now: now + 600))
        #expect(watch.roots == [cache])
        #expect(Files.writeAtomic(cache + "/other.json", "x"))
        watch.event(cache, flags: 0)
        Files.mkdirs(cache + "/elsewhere")
        watch.event(cache, flags: 0)
        #expect(!watch.dirty)
        Files.mkdirs(cache + "/perf")
        watch.event(cache, flags: 0)
        #expect(watch.dirty)
        watch.stop()
    }
}

@Test("a storm of events after the watch is due costs a flag check; a moved root still re-arms; nested folders add no root")
func capsEventCost() throws {
    let (home, watch, queue) = try capsHome("storm", caps: #"[{"path": "HOME/caps/*/builds", "max_gb": 10}]"#)
    defer { try? FileManager.default.removeItem(atPath: home) }
    Files.mkdirs(home + "/caps/a/builds")
    let prefix = Files.realpath(home + "/caps")
    let now = Kernel.wall()
    queue.sync {
        _ = watch.due(now: now)
        watch.started(now: now)
        // the run reports a plain folder under the prefix: no root of its own
        watch.finished(PyObject([("busy", 0), ("folders", .array([.string(prefix + "/a/builds")]))]))
        #expect(!watch.due(now: now + 600))
        #expect(watch.prefixes == [prefix])
        watch.event(prefix + "/a/builds/x", flags: 0)
        #expect(watch.dirty)
        watch.event(prefix + "/elsewhere", flags: FSEventStreamEventFlags(kFSEventStreamEventFlagRootChanged))
        #expect(watch.rearm)
        watch.stop()
    }
}
