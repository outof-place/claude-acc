import AccKit
import Foundation
import Testing
@testable import ClaudeAcc

// Pod Menu's side of the menu bar hand-over (MenuBarPresence over AccKit's AccMenuBarCoordinator),
// on a temporary $STATE with fake parties: no real app, no status item.

private let podMenuParty = AccMenuBarParty(role: .podMenu, bundleId: AccMenuBarOwnership.podMenuBundleId, pid: 501, at: 0)
private let nativeParty = AccMenuBarParty(role: .podNative, bundleId: "codes.pod.native", pid: 601, at: 0)
private let nativeCopy = AccMenuBarParty(role: .podNative, bundleId: "codes.pod.native", pid: 602, at: 0)

private func tempPaths() throws -> AccPaths {
    let home = FileManager.default.temporaryDirectory.appending(path: "pod-menu-bar-\(UUID().uuidString)")
    let paths = AccPaths(home: home)
    try FileManager.default.createDirectory(at: paths.state, withIntermediateDirectories: true)
    return paths
}

/// What MenuBarController was told, and what menubar.json said at that moment.
@MainActor private final class Ring {
    var shown = false
    var changes: [(shown: Bool, owner: Int32?)] = []
    let ownership: AccMenuBarOwnership
    init(_ ownership: AccMenuBarOwnership) { self.ownership = ownership }
    func set(_ shown: Bool) {
        self.shown = shown
        changes.append((shown, ownership.read().owner?.pid))
    }
}

@MainActor private func eventually(_ seconds: Double = 3, _ condition: () -> Bool) async -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while !condition() {
        if Date() > deadline { return false }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return true
}

/// Pod's nested Pod Menu over `paths`, as `podMenuParty`.
@MainActor private func nestedPresence(
    _ paths: AccPaths, alive: @escaping @Sendable (AccMenuBarParty) -> Bool
) -> (MenuBarPresence, Ring, AccStore, AccMenuBarOwnership) {
    let menuPid = podMenuParty.pid
    let ownership = AccMenuBarOwnership(
        paths: paths, ackTimeout: 2, retryDelay: 30, alive: alive, podMenuPids: { [menuPid] })
    let ring = Ring(ownership)
    let store = AccStore(paths: paths, interest: [.menuBar])
    let handover = AccMenuBarCoordinator(store: store, me: podMenuParty, ownership: ownership) { ring.set($0) }
    return (MenuBarPresence(store: store, handover: handover) { ring.set($0) }, ring, store, ownership)
}

@MainActor
@Suite struct MenuBarPresenceTests {
    @Test("the standalone copy always shows its ring and never writes menubar.json")
    func standaloneAlwaysShows() throws {
        let paths = try tempPaths()
        // a stale file from a Pod install: a live native shell owns the item there
        let other = AccMenuBarOwnership(paths: paths, ackTimeout: 2, retryDelay: 30, alive: { _ in true })
        try other.setHost(.podNative)
        _ = try other.step(as: nativeParty)
        let before = try Data(contentsOf: paths.menuBar)
        var shown: [Bool] = []
        let presence = MenuBarPresence(nested: false) { shown.append($0) }
        #expect(!presence.takesPart)
        presence.start()
        presence.stop()
        #expect(shown == [true])
        #expect(try Data(contentsOf: paths.menuBar) == before)
        #expect(!PodMenu.active)  // a test run is the standalone copy
    }

    @Test("a corrupt or unknown menubar.json leaves Pod Menu showing its ring")
    func corruptFileShows() async throws {
        for junk in ["{not json", #"{"version": 9, "host": "somewhere-new", "owner": {"role": "nope"}}"#] {
            let paths = try tempPaths()
            try Data(junk.utf8).write(to: paths.menuBar)
            let (presence, ring, _, _) = nestedPresence(paths, alive: { _ in true })
            presence.start()
            #expect(ring.shown, "\(junk)")
            presence.stop()
        }
    }

    @Test("on quit the ring comes off first, then the hand-over releases it")
    func quitHidesThenReleases() async throws {
        let paths = try tempPaths()
        let (presence, ring, _, ownership) = nestedPresence(paths, alive: { _ in true })
        presence.start()
        #expect(ring.shown)
        presence.stop()
        #expect(!ring.shown)
        // when the ring came off, menubar.json still named Pod Menu: the release came after
        #expect(ring.changes.last?.shown == false && ring.changes.last?.owner == podMenuParty.pid)
        #expect(ownership.read().owner == nil)
    }

    @Test("the native shell's exit gives the ring back at once")
    func ownerExitGivesTheRingBack() async throws {
        let paths = try tempPaths()
        let child = Process()
        child.executableURL = URL(fileURLWithPath: "/bin/sleep")
        child.arguments = ["0.4"]
        try child.run()
        let native = AccMenuBarParty(role: .podNative, bundleId: "codes.pod.native", pid: child.processIdentifier, at: 0)
        let menuPid = podMenuParty.pid
        let alive: @Sendable (AccMenuBarParty) -> Bool = { $0.pid == menuPid || kill($0.pid, 0) == 0 }
        let other = AccMenuBarOwnership(paths: paths, ackTimeout: 2, retryDelay: 30, alive: alive)
        try other.setHost(.podNative)
        #expect(try other.step(as: native) == .show)
        let (presence, ring, _, _) = nestedPresence(paths, alive: alive)
        presence.start()
        #expect(!ring.shown)
        child.waitUntilExit()
        #expect(await eventually { ring.shown })
        presence.stop()
    }

    @Test("two native shells racing for the ring: Pod Menu hands it to one, and only one shows")
    func twoNativeShellsRace() async throws {
        let paths = try tempPaths()
        let running: Set<Int32> = [podMenuParty.pid, nativeParty.pid, nativeCopy.pid]
        let alive: @Sendable (AccMenuBarParty) -> Bool = { running.contains($0.pid) }
        let (presence, ring, store, ownership) = nestedPresence(paths, alive: alive)
        presence.start()
        #expect(ring.shown)
        try ownership.setHost(.podNative)
        // both claim before Pod Menu looks: the first claim stands, the second waits its turn
        let first = try ownership.step(as: nativeParty)
        let second = try ownership.step(as: nativeCopy)
        #expect(first == .waiting(recheckAfter: .seconds(2)))
        #expect(second == .hide)
        await store.reload()
        #expect(await eventually { !ring.shown })
        #expect(try ownership.step(as: nativeParty) == .show)
        #expect(try ownership.step(as: nativeCopy) == .hide)
        presence.stop()
        #expect(!ring.shown)
    }
}
