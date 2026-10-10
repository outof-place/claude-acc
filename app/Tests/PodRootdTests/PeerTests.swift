import Foundation
import LightweightCodeRequirements
import Security
import Testing
import XPC
@testable import PodRootdCore
import PodRootdClient

// The XPC side for real, in one process: an anonymous listener with the engine on the fake machine,
// and PodRootdClient on an endpoint. The test runner is not signed by Pod's team, which is exactly
// the peer the helper has to turn away.

/// The signing identifier of this test process (ad hoc: swiftpm-testing-helper-<hash>).
private func ownIdentifier() throws -> String {
    var code: SecCode?
    var staticCode: SecStaticCode?
    var info: CFDictionary?
    try #require(SecCodeCopySelf([], &code) == errSecSuccess)
    try #require(SecCodeCopyStaticCode(try #require(code), [], &staticCode) == errSecSuccess)
    try #require(SecCodeCopySigningInformation(
        try #require(staticCode), SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess)
    return try #require((info as? [String: Any])?[kSecCodeInfoIdentifier as String] as? String)
}

/// A policy this process passes, as the given caller.
private func admitting(as caller: Caller) throws -> PeerPolicy {
    let me = try ownIdentifier()
    let requirement = XPCPeerRequirement.codeRequirement(try ProcessCodeRequirement.allOf { SigningIdentifier(me) })
    return PeerPolicy(listener: requirement, callers: [(caller, requirement)])
}

private final class Wire {
    let rig: Rig
    let server: Server
    let client: PodRootdClient

    init(peers: PeerPolicy, rig: Rig = Rig()) throws {
        self.rig = rig
        server = Server(engine: rig.engine, peers: peers)
        client = try PodRootdClient(endpoint: server.listenAnonymously())
    }

    func close() {
        client.close()
        server.cancel()
    }
}

/// Until `condition` holds or a second goes by, letting the main queue run the session's end.
private func eventually(_ condition: () -> Bool) async {
    for _ in 0..<100 where !condition() { try? await Task.sleep(for: .milliseconds(10)) }
}

@Test("a peer outside Pod's team is turned away before any verb, and learns nothing")
func foreignPeerRejected() async throws {
    let wire = try Wire(peers: try PeerPolicy.production())
    defer { wire.close() }
    let reply = try await wire.client.send(.fansSet(mode: fan50))
    #expect(reply.refusal == .peerNotAllowed)
    #expect(reply.status == nil)
    #expect(wire.rig.backend.calls.isEmpty)
    let status = try await wire.client.send(.status)
    #expect(status.refusal == .peerNotAllowed)
    #expect(status.status == nil)
}

@Test("the production policy names the three signing identities of team 75Y2KR6P5W")
func productionCallers() throws {
    let policy = try PeerPolicy.production()
    #expect(policy.callers.map(\.0) == [.app, .menu, .cli])
    // nothing here is signed by that team, so no identity matches
    #expect(policy.classify { _ in false } == nil)
    #expect(policy.classify { _ in true } == .app)
}

@Test("an admitted peer gets the verb done and the status back over XPC")
func admittedPeer() async throws {
    let wire = try Wire(peers: try admitting(as: .menu))
    defer { wire.close() }
    let reply = try await wire.client.run(.fansSet(mode: fan50))
    #expect(reply.status?.fans.mode == fan50)
    #expect(wire.rig.backend.calls == ["applyFans 50%"])
    #expect(try await wire.client.status().fans.applied == fan50)
}

@Test("over XPC: Electron Pod gets tier A only, Pod Menu tier B on a click, the CLI tier B only approved")
func tiersByCaller() async throws {
    let app = try Wire(peers: try admitting(as: .app))
    defer { app.close() }
    #expect(try await app.client.send(.spotlightAppsOnly).refusal == .verbNotAllowed(verb: "spotlight.appsOnly", caller: .app))
    #expect(try await app.client.send(.fansSet(mode: .auto)).refusal == nil)
    let menu = try Wire(peers: try admitting(as: .menu))
    defer { menu.close() }
    #expect(try await menu.client.send(.spotlightAppsOnly).refusal == nil)
    let cli = try Wire(peers: try admitting(as: .cli))
    defer { cli.close() }
    #expect(try await cli.client.send(.spotlightRestore).refusal == .needsApproval(verb: "spotlight.restore"))
    let approved = Approval(authenticated: true, parent: "sid=1 ppid=2@3 tty=ttys001")
    #expect(try await cli.client.send(.spotlightRestore, approval: approved).refusal == nil)
}

@Test("closing the client ends its lid hold: a quit or crashed Pod Menu can't keep the Mac from sleeping")
func leaseEndsWithSession() async throws {
    let wire = try Wire(peers: try admitting(as: .menu))
    defer { wire.server.cancel() }
    try await wire.client.run(.lidHold(seconds: LidSeconds(3600)!))
    #expect(wire.rig.backend.sleepIsDisabled)
    #expect(wire.server.sessions == 1)
    wire.client.close()
    await eventually { wire.server.sessions == 0 }
    #expect(wire.server.sessions == 0)
    #expect(!wire.rig.backend.sleepIsDisabled)
    #expect(wire.rig.engine.status().lid.lastRelease == "session ended")
}
