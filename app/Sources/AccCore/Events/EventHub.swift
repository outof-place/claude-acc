// claude-acc's state files as a stream, for hosts (AccKit's AccStore in PodNative, Pod Menu) that
// would otherwise watch and re-read them each. A client on the control socket sends the line
// `{"op":"events","v":1}`; from then on both directions are frames:
//   u32 little-endian length of what follows, u8 kind, payload
//   1 hello      server: {"v": 1, "topics": [names]}
//   2 subscribe  client: {"topics": [names], "have": {name: seq}, "maxHz": {name: hz}}; the whole
//                set, replacing the last one (a hidden window keeps only what its menu bar needs)
//   3 snapshot   server: u16 little-endian header length, header {"topic", "seq"}, then the file's
//                exact bytes, so a host decodes them with the Decodable it uses on the file
//   4 gone       server: {"topic"}: the file isn't there (or isn't a regular file of at most 1 MiB)
//   5 error      server: {"code", "msg"}
// `seq` is the file's "ino:mtime_ns:size", the same stamp a host computes when it reads the file
// itself (from st_mtimespec, in ns), so `have` works across a fallback to files. Each topic is
// latest-wins per client: while a client hasn't taken what was sent, newer versions replace older
// ones instead of queueing, and `maxHz` (clamped to 0.001-1000) spaces a topic's frames. The files
// stay the contract, written atomically (a rename): Python writes them, acc-cored only watches them
// (their folders for renames, each subscribed file for writes in place), and only while someone
// subscribes. `pressure` is derived: devguard-state.json's snapshot.pressure.level as {"level": N},
// sent when the level changes, so a menu bar doesn't decode 30 KB to badge a ring; its seq is
// devguard-state.json's.
// A client that hasn't subscribed `subscribeSeconds` after the events line, or whose socket took
// nothing for `stallSeconds` while frames waited (it stopped reading), is dropped; the reaper looks
// every `reapEvery`, so that's up to one more period. A slow reader that keeps taking stays.
import Darwin
import Dispatch
import Synchronization

public final class EventHub: @unchecked Sendable {
    /// topic -> file under the state directory
    public static let files: [(topic: String, path: String)] = [
        ("accounts", "status.json"), ("accountsSlot", "accswitch-slot.json"), ("devguard", "devguard-state.json"),
        ("sched", "sched/state.json"), ("depot", "sched/depot.json"), ("awake", "awake-state.json"),
        ("services", "pod-services.json"), ("owner", "owner.json"), ("menuBar", "menubar.json"),
        ("janitor", "janitor-state.json"), ("fans", "fans-state.json"), ("hotspot", "hotspot-state.json"),
    ]
    public static let derived = ["pressure"]
    public static var topics: [String] { files.map(\.topic) + derived }

    public static let version = 1
    public var maxClients = 16
    /// a client with this much unsent when the socket is full is dropped (one flush of every topic
    /// at `maxFile` fits)
    public var maxOutbox = 16 << 20
    /// a client whose frames haven't drained for this long has stopped reading
    public var stallSeconds = 30.0
    /// a client that hasn't subscribed this long after the events line
    public var subscribeSeconds = 10.0
    /// how often the two above are checked (only while there are clients)
    var reapEvery = 5.0
    static let maxFrame = 1 << 20
    /// a state file past this isn't one: sent as gone
    static let maxFile = 1 << 20
    /// maxHz is clamped to this: a subnormal rate made 1 / hz infinite
    static let hzRange = 0.001...1000.0

    let stateDir: String
    let queue = DispatchQueue(label: "acc-cored.events", qos: .utility)
    /// the number of clients, for `ping` without the hub's queue (a slow read may hold it)
    let clientCount = Atomic<Int>(0)

    /// One subscriber; touched only on `queue`.
    final class Client: @unchecked Sendable {
        let fd: Int32
        let adopted: Double
        var reader: DispatchSourceRead?
        var writer: DispatchSourceWrite?
        var inbox: [UInt8] = []
        /// frames on their way; a snapshot's bytes are shared with every client it goes to
        var outbox: [[UInt8]] = []
        /// how much of `outbox[0]` the socket took, and what's left of all of them
        var sent = 0
        var queued = 0
        var topics: Set<String> = []
        var have: [String: String] = [:]
        var maxHz: [String: Double] = [:]
        var lastSent: [String: Double] = [:]
        /// topics whose newest version this client hasn't been sent yet
        var pending: Set<String> = []
        var subscribed = false
        /// since when the socket took nothing of what's unsent (any progress resets it)
        var stalledSince: Double?
        /// one timer at the earliest topic's next turn under maxHz
        var timer: DispatchSourceTimer?
        var timerAt = Double.infinity
        /// sources on `fd` not yet cancelled: the descriptor closes after the last one, so no
        /// source watches a number that a new connection may already hold
        var sources = 0
        var closed = false

        init(fd: Int32, at: Double) {
            self.fd = fd
            adopted = at
        }

        var unsent: Int { queued }
    }

    struct Version {
        var seq: String
        var data: [UInt8]?  // nil: gone
    }

    var clients: [Int32: Client] = [:]
    /// what each watched topic holds now
    var current: [String: Version] = [:]
    var dirSources: [String: DispatchSourceFileSystemObject] = [:]
    /// for a missing folder: its nearest existing parent, and the source there
    var parentSources: [String: (parent: String, source: DispatchSourceFileSystemObject)] = [:]
    var fileSources: [String: DispatchSourceFileSystemObject] = [:]
    var reaper: DispatchSourceTimer?
    /// each topic's snapshot frame, built once per version and shared by the clients it goes to
    var frames: [String: (seq: String, bytes: [UInt8])] = [:]
    /// one read buffer for every client: reads run on `queue` only
    var chunk = [UInt8](repeating: 0, count: 64 * 1024)
    public private(set) var framesSent = 0

    public init(stateDir: String) {
        self.stateDir = stateDir
    }

    /// Takes over a connection that asked for events, with what it already sent after its line.
    public func adopt(_ fd: Int32, leftover: [UInt8]) {
        queue.async { [weak self] in
            guard let self else {
                close(fd)
                return
            }
            // XNU refuses SO_NOSIGPIPE when the peer is gone already: closed without a write
            var on: Int32 = 1
            guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size)) == 0 else {
                close(fd)
                return
            }
            guard self.clients.count < self.maxClients else {
                Self.sendNow(fd, Self.frame(5, Self.json([("code", "busy"), ("msg", "too many event clients")])))
                close(fd)
                return
            }
            _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
            let c = Client(fd: fd, at: Kernel.monotonic())
            self.clients[fd] = c
            self.clientCount.store(self.clients.count, ordering: .relaxed)
            let reader = self.tracked(c, DispatchSource.makeReadSource(fileDescriptor: fd, queue: self.queue))
            reader.setEventHandler { [weak self, c] in self?.readable(c) }
            c.reader = reader
            reader.resume()
            self.armReaper()
            self.enqueue(c, Self.frame(1, Self.json([("v", .int(Self.version)), ("topics", .array(Self.topics.map { .string($0) }))])))
            c.inbox = leftover
            self.parse(c)
        }
    }

    public func stop() {
        queue.sync {
            for c in Array(clients.values) { drop(c) }
            rewatch()
        }
    }

    /// The number of event clients, for `ping`.
    public var count: Int { clientCount.load(ordering: .relaxed) }

    // MARK: frames

    static func frame(_ kind: UInt8, _ payload: [UInt8]) -> [UInt8] {
        let n = UInt32(payload.count + 1)
        return [UInt8(n & 0xFF), UInt8(n >> 8 & 0xFF), UInt8(n >> 16 & 0xFF), UInt8(n >> 24), kind] + payload
    }

    static func json(_ fields: [(String, PyJSON)]) -> [UInt8] {
        Array(PyJSON.object(PyObject(fields)).dumps().utf8)
    }

    static func snapshot(_ topic: String, _ v: Version) -> [UInt8] {
        let header = json([("topic", .string(topic)), ("seq", .string(v.seq))])
        let n = UInt16(header.count)
        return frame(3, [UInt8(n & 0xFF), UInt8(n >> 8)] + header + (v.data ?? []))
    }

    func readable(_ c: Client) {
        let n = chunk.withUnsafeMutableBytes { Darwin.read(c.fd, $0.baseAddress, $0.count) }
        if n <= 0 {
            if n < 0 && (errno == EAGAIN || errno == EINTR) { return }
            drop(c)
            return
        }
        c.inbox += chunk[..<n]
        parse(c)
    }

    func parse(_ c: Client) {
        guard !c.closed else { return }  // a hello that failed dropped it already
        var at = 0
        defer { if at > 0, !c.closed { c.inbox.removeFirst(at) } }
        while c.inbox.count - at >= 5 {
            let n = Int(c.inbox[at]) | Int(c.inbox[at + 1]) << 8 | Int(c.inbox[at + 2]) << 16 | Int(c.inbox[at + 3]) << 24
            guard n >= 1, n <= Self.maxFrame else {
                fail(c, "frame", "a frame of \(n) bytes")
                return
            }
            guard c.inbox.count - at >= 4 + n else { return }
            let kind = c.inbox[at + 4]
            let payload = Array(c.inbox[(at + 5)..<(at + 4 + n)])
            at += 4 + n
            guard kind == 2, let request = (try? PyJSON.loads(bytes: payload))?.object else {
                fail(c, "frame", "only subscribe (2) with a JSON object is understood")
                return
            }
            subscribe(c, request)
            if c.closed { return }
        }
    }

    func fail(_ c: Client, _ code: String, _ msg: String) {
        Self.sendNow(c.fd, Self.frame(5, Self.json([("code", .string(code)), ("msg", .string(msg))])))
        drop(c)
    }

    // MARK: subscriptions

    func subscribe(_ c: Client, _ request: PyObject) {
        let asked = (request["topics"]?.array ?? []).compactMap(\.string)
        let unknown = asked.filter { !Self.topics.contains($0) }
        if !unknown.isEmpty {
            enqueue(c, Self.frame(5, Self.json([("code", "topic"), ("msg", .string("unknown: " + unknown.joined(separator: ", ")))])))
            if c.closed { return }
        }
        c.topics = Set(asked).subtracting(unknown)
        c.subscribed = true
        // what the client says it holds now: a topic it dropped and asks for again is sent again
        c.have = [:]
        for (topic, seq) in request["have"]?.object ?? PyObject() {
            if let s = seq.string { c.have[topic] = s }
        }
        c.maxHz = [:]
        for (topic, hz) in request["maxHz"]?.object ?? PyObject() {
            if let h = hz.double, h > 0, h.isFinite {
                c.maxHz[topic] = min(max(h, Self.hzRange.lowerBound), Self.hzRange.upperBound)
            }
        }
        c.pending = c.pending.intersection(c.topics)
        rewatch()
        // a stamp for each (a stat): what the hub remembers may be from before a folder came
        var moved: Set<String> = []
        for topic in c.topics { moved.formUnion(refresh(topic)) }
        for topic in c.topics {
            if let v = current[topic], c.have[topic] != v.seq { c.pending.insert(topic) }
        }
        notify(moved)
        flush(c)
    }

    /// The topics anyone subscribes to, with what they need watched (pressure needs devguard).
    var watchedTopics: Set<String> {
        var out = Set(clients.values.flatMap(\.topics))
        if out.contains("pressure") { out.insert("devguard") }
        return out
    }

    func path(_ topic: String) -> String? {
        Self.files.first { $0.topic == topic }.map { stateDir + "/" + $0.path }
    }

    /// Folder sources for renames and creations, a source per file for writes in place; only for
    /// what someone subscribes to.
    func rewatch() {
        let topics = watchedTopics
        let paths = Set(topics.compactMap(path))
        let dirs = Set(paths.map(dirname))
        for dir in Array(dirSources.keys) where !dirs.contains(dir) {
            dirSources[dir]?.cancel()
            dirSources[dir] = nil
        }
        for dir in Array(parentSources.keys) where !dirs.contains(dir) {
            parentSources[dir]?.source.cancel()
            parentSources[dir] = nil
        }
        for file in Array(fileSources.keys) where !paths.contains(file) {
            fileSources[file]?.cancel()
            fileSources[file] = nil
        }
        for topic in Array(current.keys) where !topics.contains(topic) {
            current[topic] = nil
            frames[topic] = nil
        }
        for dir in dirs where dirSources[dir] == nil && parentSources[dir] == nil { armDir(dir) }
        for file in paths where fileSources[file] == nil { watchFile(file) }
    }

    /// A folder's source, for renames and creations in it. A missing folder is waited for from its
    /// nearest existing parent. Every (re)arm watches its files again and re-stamps their topics:
    /// a folder made, replaced or moved back holds other files than the old descriptors see.
    func armDir(_ dir: String) {
        dirSources[dir]?.cancel()
        dirSources[dir] = nil
        parentSources[dir]?.source.cancel()
        parentSources[dir] = nil
        if let source = watch(dir, [.write, .delete, .rename], { [weak self] events in
            guard let self else { return }
            if !events.isDisjoint(with: [.delete, .rename]) {
                self.armDir(dir)
            } else {
                self.changed(in: dir)
            }
        }) {
            dirSources[dir] = source
        } else {
            let parent = Self.nearest(dir)
            if let source = watch(parent, [.write, .delete, .rename], { [weak self] _ in
                // a folder on the way came or went: arm again (the folder itself, or a nearer parent)
                guard let self, Files.isDirectory(dir) || Self.nearest(dir) != parent else { return }
                self.armDir(dir)
            }) {
                parentSources[dir] = (parent, source)
            }
        }
        for file in Array(fileSources.keys) where dirname(file) == dir {
            fileSources[file]?.cancel()
            fileSources[file] = nil
        }
        changed(in: dir)
    }

    /// The nearest existing folder at or above `dir`'s parent.
    static func nearest(_ dir: String) -> String {
        var up = dirname(dir)
        while up != "/", !Files.isDirectory(up) { up = dirname(up) }
        return up
    }

    func watchFile(_ file: String) {
        fileSources[file] = watch(file, [.write, .extend, .attrib, .delete, .rename]) { [weak self] events in
            guard let self else { return }
            // replaced or removed: this descriptor sees the old file; watch the path again
            if !events.isDisjoint(with: [.delete, .rename]) {
                self.fileSources[file]?.cancel()
                self.fileSources[file] = nil
                if self.watchedTopics.compactMap(self.path).contains(file) { self.watchFile(file) }
            }
            self.changed(file: file)
        }
    }

    func watch(_ path: String, _ mask: DispatchSource.FileSystemEvent, _ handler: @escaping @Sendable (DispatchSource.FileSystemEvent) -> Void)
        -> DispatchSourceFileSystemObject?
    {
        // O_NONBLOCK: a FIFO in place of a state file doesn't hold the queue in open()
        let fd = open(path, O_EVTONLY | O_CLOEXEC | O_NONBLOCK)
        guard fd >= 0 else { return nil }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: mask, queue: queue)
        source.setEventHandler { [weak source] in handler(source?.data ?? []) }
        source.setCancelHandler { close(fd) }
        source.resume()
        return source
    }

    /// Something in `dir` changed: watch its files that came, re-stamp its topics, send what moved.
    func changed(in dir: String) {
        let topics = watchedTopics
        for topic in topics {
            if let p = path(topic), dirname(p) == dir, fileSources[p] == nil { watchFile(p) }
        }
        var moved: Set<String> = []
        for topic in topics where path(topic).map(dirname) == dir { moved.formUnion(refresh(topic)) }
        notify(moved)
    }

    /// A write in place: only that file's topic is stamped again.
    func changed(file: String) {
        var moved: Set<String> = []
        for topic in watchedTopics where path(topic) == file { moved.formUnion(refresh(topic)) }
        notify(moved)
    }

    func notify(_ moved: Set<String>) {
        guard !moved.isEmpty else { return }
        for c in Array(clients.values) {
            let mine = c.topics.intersection(moved)
            guard !mine.isEmpty else { continue }
            c.pending.formUnion(mine)
            flush(c)
        }
    }

    /// Stamps the topic's file (a stat) and reads it only when the stamp moved; the topics that moved
    /// (devguard brings pressure along).
    func refresh(_ topic: String) -> [String] {
        if topic == "pressure" {
            var moved = refresh("devguard")
            if !moved.contains("pressure"), refreshPressure() { moved.append("pressure") }
            return moved
        }
        guard let p = path(topic) else { return [] }
        if let old = current[topic], old.seq == Self.stamp(p) { return [] }
        let v = Self.read(p)
        if let old = current[topic], old.seq == v.seq { return [] }
        current[topic] = v
        var moved = [topic]
        if topic == "devguard", watchedTopics.contains("pressure"), refreshPressure() { moved.append("pressure") }
        return moved
    }

    /// pressure from devguard's snapshot: a new version only when the level changes
    func refreshPressure() -> Bool {
        guard let devguard = current["devguard"] else { return false }
        var v = Version(seq: devguard.seq, data: nil)
        if let data = devguard.data, let level = (try? PyJSON.loads(bytes: data))?["snapshot"]?["pressure"]?["level"]?.int {
            v.data = Array("{\"level\":\(level)}".utf8)
        }
        if let old = current["pressure"], old.data == v.data { return false }
        current["pressure"] = v
        return true
    }

    static func seq(_ st: stat) -> String {
        let ns = Int64(st.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(st.st_mtimespec.tv_nsec)
        return "\(st.st_ino):\(ns):\(st.st_size)"
    }

    /// A state file is a regular file (not followed through a link) of at most `maxFile`.
    static func isStateFile(_ st: stat) -> Bool {
        st.st_mode & S_IFMT == S_IFREG && st.st_size <= maxFile
    }

    /// The file's stamp without reading it; "" when it isn't a state file.
    static func stamp(_ path: String) -> String {
        var st = stat()
        guard lstat(path, &st) == 0, isStateFile(st) else { return "" }
        return seq(st)
    }

    /// The file's bytes and its stamp from the same descriptor; "" and nil when it isn't a state
    /// file (missing, a link, a FIFO that would block the read, a device, past `maxFile`).
    static func read(_ path: String) -> Version {
        let gone = Version(seq: "", data: nil)
        let fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW)
        guard fd >= 0 else { return gone }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0, isStateFile(st) else { return gone }
        var data = [UInt8](repeating: 0, count: Int(st.st_size))
        var got = 0
        while got < data.count {
            let n = data.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress! + got, $0.count - got) }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            got += n
        }
        return Version(seq: seq(st), data: Array(data[..<got]))
    }

    // MARK: sending

    /// Sends each pending topic's newest version, as `maxHz` allows; nothing while the last frames
    /// are still on their way, so a slow client gets the latest instead of a queue.
    func flush(_ c: Client) {
        guard c.unsent == 0, !c.closed else { return }
        let now = Kernel.monotonic()
        var wait = Double.infinity
        for topic in c.pending.sorted() {
            if let hz = c.maxHz[topic], let last = c.lastSent[topic], now - last < 1 / hz {
                wait = min(wait, last + 1 / hz - now)
                continue
            }
            c.pending.remove(topic)
            guard let v = current[topic], c.have[topic] != v.seq else { continue }
            append(c, v.data == nil ? Self.frame(4, Self.json([("topic", .string(topic))])) : snapshotFrame(topic, v))
            c.have[topic] = v.seq
            c.lastSent[topic] = now
            framesSent += 1
        }
        if wait < .infinity { schedule(c, at: now + wait) }
        write(c, flushAfter: false)
    }

    /// The client's one timer goes to the earliest topic's turn: a slower topic doesn't push back a
    /// faster one's.
    func schedule(_ c: Client, at: Double) {
        guard at < c.timerAt else { return }
        c.timerAt = at
        let t: DispatchSourceTimer
        if let timer = c.timer {
            t = timer
        } else {
            t = DispatchSource.makeTimerSource(queue: queue)
            t.setEventHandler { [weak self, c] in
                c.timerAt = .infinity
                self?.flush(c)
            }
            c.timer = t
            t.resume()
        }
        t.schedule(deadline: .now() + max(0, at - Kernel.monotonic()), leeway: .milliseconds(20))
    }

    func snapshotFrame(_ topic: String, _ v: Version) -> [UInt8] {
        if let f = frames[topic], f.seq == v.seq { return f.bytes }
        let bytes = Self.snapshot(topic, v)
        frames[topic] = (v.seq, bytes)
        return bytes
    }

    func append(_ c: Client, _ frame: [UInt8]) {
        c.outbox.append(frame)
        c.queued += frame.count
    }

    func enqueue(_ c: Client, _ frame: [UInt8]) {
        guard !c.closed else { return }
        append(c, frame)
        write(c)
    }

    /// As much of the outbox as the socket takes now; the rest when it's writable again. A drained
    /// outbox sends what's pending, except when the call comes from flush itself.
    func write(_ c: Client, flushAfter: Bool = true) {
        guard !c.closed else { return }
        while let first = c.outbox.first {
            let w = first.withUnsafeBytes { Darwin.send(c.fd, $0.baseAddress! + c.sent, $0.count - c.sent, MSG_NOSIGNAL) }
            if w > 0 {
                c.sent += w
                c.queued -= w
                c.stalledSince = nil  // it reads, however slowly
                if c.sent == first.count {
                    c.outbox.removeFirst()
                    c.sent = 0
                }
                continue
            }
            if w < 0 && errno == EINTR { continue }
            guard w < 0 && errno == EAGAIN, c.unsent <= maxOutbox else {
                drop(c)
                return
            }
            if c.stalledSince == nil { c.stalledSince = Kernel.monotonic() }
            if c.writer == nil {
                let writer = tracked(c, DispatchSource.makeWriteSource(fileDescriptor: c.fd, queue: queue))
                writer.setEventHandler { [weak self, c] in
                    c.writer?.cancel()
                    c.writer = nil
                    self?.write(c)
                }
                c.writer = writer
                writer.resume()
            }
            return
        }
        c.stalledSince = nil
        if flushAfter, !c.pending.isEmpty { flush(c) }
    }

    /// A short frame before a close: best effort, no waiting.
    static func sendNow(_ fd: Int32, _ frame: [UInt8]) {
        _ = frame.withUnsafeBytes { Darwin.send(fd, $0.baseAddress, $0.count, MSG_NOSIGNAL) }
    }

    // MARK: lifetime

    /// A source on the client's descriptor: its cancel closes the descriptor once the client is
    /// dropped and no other source is left on it.
    func tracked<S: DispatchSourceProtocol>(_ c: Client, _ source: S) -> S {
        c.sources += 1
        source.setCancelHandler { [c] in
            c.sources -= 1
            if c.closed, c.sources == 0 { close(c.fd) }
        }
        return source
    }

    func drop(_ c: Client) {
        guard clients[c.fd] === c, !c.closed else { return }
        clients[c.fd] = nil
        clientCount.store(clients.count, ordering: .relaxed)
        c.closed = true
        c.timer?.cancel()
        c.timer = nil
        c.writer?.cancel()
        c.writer = nil
        c.reader?.cancel()
        c.reader = nil
        if c.sources == 0 { close(c.fd) }
        if clients.isEmpty {
            reaper?.cancel()
            reaper = nil
        }
        rewatch()
    }

    /// Drops clients that never subscribed or stopped reading; runs only while there are clients.
    func armReaper() {
        guard reaper == nil else { return }
        let r = DispatchSource.makeTimerSource(queue: queue)
        r.schedule(deadline: .now() + reapEvery, repeating: reapEvery, leeway: .milliseconds(200))
        r.setEventHandler { [weak self] in
            guard let self else { return }
            let now = Kernel.monotonic()
            for c in Array(self.clients.values) {
                let silent = !c.subscribed && now - c.adopted > self.subscribeSeconds
                let stalled = c.stalledSince.map { now - $0 > self.stallSeconds } ?? false
                if silent || stalled { self.drop(c) }
            }
        }
        r.resume()
        reaper = r
    }
}
