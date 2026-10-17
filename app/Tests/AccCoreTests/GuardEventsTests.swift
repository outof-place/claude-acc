import Darwin
import Foundation
import Testing
@testable import AccCore

// The guard's rows in the decision registry ($STATE/events.jsonl, events.py) and the inventory ring
// the `stage` row looks back at (devguard_core.remember_inventory). Every test writes to a scratch
// directory of its own.

private func scratch() throws -> String {
    let dir = FileManager.default.temporaryDirectory.appending(path: "guard-events-\(UUID().uuidString)").path
    try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    return dir
}

private func lines(_ path: String) -> [String] {
    ((try? String(contentsOfFile: path, encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
}

@Test("a row: the clocks and the kind first, then the fields as json.dumps(ensure_ascii=False) writes them")
func eventsRowShape() throws {
    let dir = try scratch()
    let fields = PyObject([("ts", .int(0)), ("reason", .string("pamięć \"ciasno\"\n")), ("stage", .int(2))])
    #expect(GuardEvents.append(stateDir: dir, ev: "stage", fields: fields, now: 1_792_200_000.5, up: 93085.25))
    let rows = lines(dir + "/events.jsonl")
    try #require(rows.count == 1)
    let boot = GuardEvents.boot.map { "\"\($0)\"" } ?? "null"
    #expect(rows[0] == #"{"ts": 1792200000.5, "up": 93085.25, "boot": \#(boot), "ev": "stage", "reason": "pamięć \"ciasno\"\n", "stage": 2}"#)
    // a second row goes after the first, in the same file
    #expect(GuardEvents.append(stateDir: dir, ev: "stage", fields: PyObject(), now: 1, up: 2))
    #expect(lines(dir + "/events.jsonl").count == 2)
}

@Test("past the limit the file goes to events.jsonl.1 before the row is written")
func eventsRotate() throws {
    let dir = try scratch()
    let path = dir + "/events.jsonl"
    try String(repeating: "x", count: 64).write(toFile: path, atomically: false, encoding: .utf8)
    #expect(GuardEvents.append(stateDir: dir, ev: "stage", fields: PyObject(), now: 1, up: 1, rotateAt: 64))
    #expect(lines(path + ".1") == [String(repeating: "x", count: 64)])
    #expect(lines(path).count == 1)
}

@Test("a lock another writer holds: no row, after about a second, never a wait without end")
func eventsLockHeld() throws {
    let dir = try scratch()
    let held = open(dir + "/events.lock", O_RDWR | O_CREAT, 0o644)
    defer { close(held) }
    try #require(flock(held, LOCK_EX) == 0)
    // flock locks the open file description: another open() of the same file contends with it
    let start = GuardEvents.uptime()
    #expect(!GuardEvents.append(stateDir: dir, ev: "stage", fields: PyObject(), now: 1, up: 1))
    let waited = GuardEvents.uptime() - start
    #expect(waited >= 0.9 && waited < 5)
    #expect(!FileManager.default.fileExists(atPath: dir + "/events.jsonl"))
}

@Test("the inventory ring: a sample at most every 30 s, the last five kept")
func inventoryRing() {
    var state = PyObject([("inventory", .object(PyObject([
        ("at", .double(100)),
        ("families", .object(PyObject([
            ("dev", .object(PyObject([("footprint", .int(7))]))), ("lsp", .object(PyObject())),
        ]))),
    ])))])
    rememberInventory(&state, now: 100)
    rememberInventory(&state, now: 129.9)
    #expect(state["inventory_ring"]?.array?.count == 1)
    let first = state["inventory_ring"]?.array?.first
    #expect(first?["at"] == .double(100))
    #expect(first?["families"] == .object(PyObject([("dev", .int(7)), ("lsp", .null)])))
    for i in 1...6 { rememberInventory(&state, now: 100 + Double(i) * 30) }
    let ring = state["inventory_ring"]?.array ?? []
    #expect(ring.map { $0["at"]?.double } == [160, 190, 220, 250, 280])
}

@Test("family_sizes: footprints by family, {} without an inventory")
func familySizesReadsFootprints() {
    #expect(PyJSON.object(familySizes(nil)) == .object(PyObject()))
    #expect(PyJSON.object(familySizes(.object(PyObject([("families", .null)])))) == .object(PyObject()))
}
