// claude-acc's state files as a stream, for hosts (AccKit's AccStore in PodNative, Pod Menu) that
// would otherwise watch and re-read them each. A client on the control socket sends the line
// `{"op":"events","v":1}`; from then on both directions are frames:
//   u32 little-endian length of what follows, u8 kind, payload
//   1 hello      server: {"v": 1, "topics": [names]}
//   2 subscribe  client: {"topics": [names], "have": {name: seq}, "maxHz": {name: hz}}; the whole
//                set, replacing the last one (a hidden window keeps only what its menu bar needs)
//   3 snapshot   server: u16 little-endian header length, header {"topic", "seq"}, then the file's
//                exact bytes, so a host decodes them with the Decodable it uses on the file
//   4 gone       server: {"topic"}: the file isn't there
//   5 error      server: {"code", "msg"}
// `seq` is the file's "ino:mtime_ns:size", the same stamp a host computes when it reads the file
// itself, so `have` works across a fallback to files. Each topic is latest-wins per client: while a
// client hasn't taken what was sent, newer versions replace older ones instead of queueing, and
// `maxHz` spaces a topic's frames. The files stay the contract: Python writes them, acc-cored only
// watches them (its directories for renames, each subscribed file for writes in place), and only
// while someone subscribes. `pressure` is derived: devguard-state.json's snapshot.pressure.level as
// {"level": N}, sent when the level changes, so a menu bar doesn't decode 30 KB to badge a ring.
import Darwin
import Dispatch

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
    /// a client that doesn't read what it gets is dropped past this much unsent
    public var maxOutbox = 16 << 20
    static let maxFrame = 1 << 20

    let stateDir: String
    let queue = DispatchQueue(label: "acc-cored.events", qos: .utility)

    /// One subscriber; touched only on `queue`.
    final class Client: @unchecked Sendable {
        let fd: Int32
        var reader: DispatchSourceRead?
        var writer: DispatchSourceWrite?
        var inbox: [UInt8] = []
        var outbox: [UInt8] = []
        var topics: Set<String> = []
        var have: [String: String] = [:]
        var maxHz: [String: Double] = [:]
        var lastSent: [String: Double] = [:]
        /// topics whose newest version this client hasn't been sent yet
        var pending: Set<String> = []
        var timer: DispatchSourceTimer?

        init(fd: Int32) { self.fd = fd }
    }

    struct Version {
        var seq: String
        var data: [UInt8]?  // nil: gone
    }

    var clients: [Int32: Client] = [:]
    /// what each watched topic holds now
    var current: [String: Version] = [:]
    var dirSources: [String: DispatchSourceFileSystemObject] = [:]
    var fileSources: [String: DispatchSourceFileSystemObject] = [:]
    public private(set) var framesSent = 0

    public init(stateDir: String) {
        self.stateDir = stateDir
    }

    /// Takes over a connection that asked for events, with what it already sent after its line.
    public func adopt(_ fd: Int32, leftover: [UInt8]) {
        queue.async {
            guard self.clients.count < self.maxClients else {
                self.sendNow(fd, Self.frame(5, Self.json([("code", "busy"), ("msg", "too many event clients")])))
                close(fd)
                return
            }
            _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
            var on: Int32 = 1
            setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
            let c = Client(fd: fd)
            self.clients[fd] = c
            let reader = DispatchSource.makeReadSource(fileDescriptor: fd, queue: self.queue)
            reader.setEventHandler { [weak self] in self?.readable(c) }
            reader.setCancelHandler { close(fd) }
            c.reader = reader
            reader.resume()
            self.enqueue(c, Self.frame(1, Self.json([("v", .int(Self.version)), ("topics", .array(Self.topics.map { .string($0) }))])))
            c.inbox = leftover
            self.parse(c)
        }
    }

    public func stop() {
        queue.sync {
            for c in clients.values { drop(c) }
            rewatch()
        }
    }

    /// The number of event clients, for `ping`.
    public var count: Int { queue.sync { clients.count } }

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
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
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
        while c.inbox.count >= 5 {
            let n = Int(c.inbox[0]) | Int(c.inbox[1]) << 8 | Int(c.inbox[2]) << 16 | Int(c.inbox[3]) << 24
            guard n >= 1, n <= Self.maxFrame else {
                fail(c, "frame", "a frame of \(n) bytes")
                return
            }
            guard c.inbox.count >= 4 + n else { return }
            let kind = c.inbox[4]
            let payload = Array(c.inbox[5..<(4 + n)])
            c.inbox.removeFirst(4 + n)
            guard kind == 2, let request = (try? PyJSON.loads(bytes: payload))?.object else {
                fail(c, "frame", "only subscribe (2) with a JSON object is understood")
                return
            }
            subscribe(c, request)
        }
    }

    func fail(_ c: Client, _ code: String, _ msg: String) {
        sendNow(c.fd, Self.frame(5, Self.json([("code", .string(code)), ("msg", .string(msg))])))
        drop(c)
    }

    // MARK: subscriptions

    func subscribe(_ c: Client, _ request: PyObject) {
        let asked = (request["topics"]?.array ?? []).compactMap(\.string)
        let unknown = asked.filter { !Self.topics.contains($0) }
        if !unknown.isEmpty {
            enqueue(c, Self.frame(5, Self.json([("code", "topic"), ("msg", .string("unknown: " + unknown.joined(separator: ", ")))])))
        }
        c.topics = Set(asked).subtracting(unknown)
        // what the client says it holds now: a topic it dropped and asks for again is sent again
        c.have = [:]
        for (topic, seq) in request["have"]?.object ?? PyObject() {
            if let s = seq.string { c.have[topic] = s }
        }
        c.maxHz = [:]
        for (topic, hz) in request["maxHz"]?.object ?? PyObject() {
            if let h = hz.double, h > 0 { c.maxHz[topic] = h }
        }
        c.pending = c.pending.intersection(c.topics)
        rewatch()
        for topic in c.topics {
            if current[topic] == nil { refresh(topic) }
            if let v = current[topic], c.have[topic] != v.seq { c.pending.insert(topic) }
        }
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

    /// Directory sources for renames and creations, a source per file for writes in place; only
    /// for what someone subscribes to.
    func rewatch() {
        let topics = watchedTopics
        let paths = Set(topics.compactMap(path))
        let dirs = Set(paths.map(dirname))
        for (dir, source) in dirSources where !dirs.contains(dir) {
            source.cancel()
            dirSources[dir] = nil
        }
        for dir in dirs where dirSources[dir] == nil {
            dirSources[dir] = watch(dir, [.write]) { [weak self] _ in self?.changed(in: dir) }
        }
        for (file, source) in fileSources where !paths.contains(file) {
            source.cancel()
            fileSources[file] = nil
        }
        for file in paths where fileSources[file] == nil { watchFile(file) }
        for topic in Array(current.keys) where !topics.contains(topic) { current[topic] = nil }
    }

    func watchFile(_ file: String) {
        fileSources[file] = watch(file, [.write, .extend, .delete, .rename]) { [weak self] events in
            guard let self else { return }
            // replaced or removed: this descriptor sees the old file; watch the path again
            if !events.isDisjoint(with: [.delete, .rename]) {
                self.fileSources[file]?.cancel()
                self.fileSources[file] = nil
                if self.watchedTopics.compactMap(self.path).contains(file) { self.watchFile(file) }
            }
            self.changed(in: dirname(file))
        }
    }

    func watch(_ path: String, _ mask: DispatchSource.FileSystemEvent, _ handler: @escaping @Sendable (DispatchSource.FileSystemEvent) -> Void)
        -> DispatchSourceFileSystemObject?
    {
        let fd = open(path, O_EVTONLY | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: mask, queue: queue)
        source.setEventHandler { [weak source] in handler(source?.data ?? []) }
        source.setCancelHandler { close(fd) }
        source.resume()
        return source
    }

    /// Something in `dir` changed: re-stamp its watched topics and send what moved.
    func changed(in dir: String) {
        let topics = watchedTopics
        // a file the watch missed because it didn't exist yet
        for topic in topics {
            if let p = path(topic), dirname(p) == dir, fileSources[p] == nil { watchFile(p) }
        }
        var moved: [String] = []
        for topic in topics where path(topic).map(dirname) == dir {
            if refresh(topic) { moved.append(topic) }
        }
        if moved.contains("devguard"), topics.contains("pressure"), refreshPressure() { moved.append("pressure") }
        guard !moved.isEmpty else { return }
        for c in clients.values {
            let mine = c.topics.intersection(moved)
            guard !mine.isEmpty else { continue }
            c.pending.formUnion(mine)
            flush(c)
        }
    }

    /// Reads the topic's file again when its stamp moved; true when it did.
    @discardableResult
    func refresh(_ topic: String) -> Bool {
        if topic == "pressure" {
            if current["devguard"] == nil { refresh("devguard") }
            return refreshPressure()
        }
        guard let p = path(topic) else { return false }
        let v = Self.read(p)
        guard current[topic]?.seq != v.seq else { return false }
        current[topic] = v
        return true
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

    /// The file's bytes and its stamp from the same descriptor; "" and nil when it isn't there.
    static func read(_ path: String) -> Version {
        let fd = open(path, O_RDONLY | O_CLOEXEC)
        guard fd >= 0 else { return Version(seq: "", data: nil) }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0 else { return Version(seq: "", data: nil) }
        var data = [UInt8](repeating: 0, count: Int(st.st_size))
        var got = 0
        while got < data.count {
            let n = data.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress! + got, $0.count - got) }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            got += n
        }
        let ns = Int64(st.st_mtimespec.tv_sec) * 1_000_000_000 + Int64(st.st_mtimespec.tv_nsec)
        return Version(seq: "\(st.st_ino):\(ns):\(st.st_size)", data: Array(data[..<got]))
    }

    // MARK: sending

    /// Sends each pending topic's newest version, as `maxHz` allows; nothing while the last frames
    /// are still on their way, so a slow client gets the latest instead of a queue.
    func flush(_ c: Client) {
        guard c.outbox.isEmpty, clients[c.fd] === c else { return }
        let now = Kernel.monotonic()
        var wait = Double.infinity
        for topic in c.pending.sorted() {
            if let hz = c.maxHz[topic], let last = c.lastSent[topic], now - last < 1 / hz {
                wait = min(wait, last + 1 / hz - now)
                continue
            }
            c.pending.remove(topic)
            guard let v = current[topic], c.have[topic] != v.seq else { continue }
            c.outbox += v.data == nil ? Self.frame(4, Self.json([("topic", .string(topic))])) : Self.snapshot(topic, v)
            c.have[topic] = v.seq
            c.lastSent[topic] = now
            framesSent += 1
        }
        if wait < .infinity && c.timer == nil {
            let t = DispatchSource.makeTimerSource(queue: queue)
            t.schedule(deadline: .now() + wait, leeway: .milliseconds(20))
            t.setEventHandler { [weak self] in
                c.timer?.cancel()
                c.timer = nil
                self?.flush(c)
            }
            t.resume()
            c.timer = t
        }
        write(c)
    }

    func enqueue(_ c: Client, _ frame: [UInt8]) {
        c.outbox += frame
        write(c)
    }

    /// As much of the outbox as the socket takes now; the rest when it's writable again.
    func write(_ c: Client) {
        while !c.outbox.isEmpty {
            let w = c.outbox.withUnsafeBytes { Darwin.write(c.fd, $0.baseAddress, $0.count) }
            if w > 0 {
                c.outbox.removeFirst(w)
                continue
            }
            if w < 0 && errno == EINTR { continue }
            guard w < 0 && errno == EAGAIN, c.outbox.count <= maxOutbox else {
                drop(c)
                return
            }
            if c.writer == nil {
                let writer = DispatchSource.makeWriteSource(fileDescriptor: c.fd, queue: queue)
                writer.setEventHandler { [weak self] in
                    c.writer?.cancel()
                    c.writer = nil
                    self?.write(c)
                }
                c.writer = writer
                writer.resume()
            }
            return
        }
        if !c.pending.isEmpty && c.timer == nil { flush(c) }
    }

    /// A short frame before a close: best effort, no waiting.
    func sendNow(_ fd: Int32, _ frame: [UInt8]) {
        _ = frame.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
    }

    func drop(_ c: Client) {
        guard clients[c.fd] === c else { return }
        clients[c.fd] = nil
        c.timer?.cancel()
        c.writer?.cancel()
        c.reader?.cancel()  // closes the descriptor
        rewatch()
    }
}
