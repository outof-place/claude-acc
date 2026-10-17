import AccKit
import Foundation
import ServiceManagement
import Synchronization
import Testing
@testable import ClaudeAcc

// pod-services.json: setup.sh's agents and this app's login item as BTM sees them, for Pod's Settings.
// Every test runs in a scratch HOME with BTM's answers stubbed: nothing asks the real BTM or writes
// the real $STATE.

nonisolated private final class Answers: Sendable {
    let agents = Mutex<[String: SMAppService.Status]>([:])
    let login = Mutex<SMAppService.Status>(.enabled)

    @MainActor func report(_ home: URL) -> ServicesReport {
        ServicesReport(
            home: home,
            legacyStatus: { url in self.agents.withLock { $0[url.lastPathComponent] } ?? .enabled },
            loginItemStatus: { self.login.withLock { $0 } })
    }
}

private func scratchHome(owner: String?) throws -> URL {
    let home = FileManager.default.temporaryDirectory
        .appending(path: "services-report-\(UUID().uuidString)", directoryHint: .isDirectory)
    let paths = AccPaths(home: home)
    try FileManager.default.createDirectory(at: paths.state, withIntermediateDirectories: true)
    if let owner {
        try Data(owner.utf8).write(to: paths.owner)
    }
    return home
}

private func read(_ home: URL) throws -> AccPodServices? {
    guard let data = try? Data(contentsOf: AccPaths(home: home).podServices) else { return nil }
    return try JSONDecoder().decode(AccPodServices.self, from: data)
}

private let podOwner = #"{"owner": "pod", "version": "1.34.2", "app": "/Applications/Pod.app/Contents/Helpers/Pod Runtime.app", "at": 1792196417}"#

@Test("the agents reported are setup.sh's JOBS, in its order")
func servicesFollowSetupJobs() throws {
    let repo = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let setup = try String(contentsOf: repo.appending(path: "setup.sh"), encoding: .utf8)
    let line = try #require(setup.split(separator: "\n").first { $0.hasPrefix("JOBS=\"") })
    let jobs = line.dropFirst("JOBS=\"".count).dropLast().split(separator: " ").map(String.init)
    #expect(jobs == ServicesReport.jobs)
}

@Test("Pod owns claude-acc: each agent and the login item with BTM's answer, as AccKit reads it")
func servicesReportForPod() async throws {
    let home = try scratchHome(owner: podOwner)
    let answers = Answers()
    answers.agents.withLock { $0 = ["com.filip.claude-acc.devguard.plist": .requiresApproval, "com.filip.claude-acc.hotspot-user.plist": .notRegistered] }
    answers.login.withLock { $0 = .notFound }
    let now = Date(timeIntervalSince1970: 1_792_200_000)
    await answers.report(home).refresh(now: now)

    let report = try #require(try read(home))
    #expect(report.version == 1)
    #expect(report.app == "/Applications/Pod.app/Contents/Helpers/Pod Runtime.app")
    #expect(report.payload == "1.34.2")
    #expect(try Date(report.at, strategy: .iso8601) == now)
    #expect(report.services.map(\.name) == ServicesReport.jobs.map { $0 + ".plist" } + ["com.filip.claude-acc.menubar"])
    #expect(report.services.dropLast().allSatisfy { $0.kind == .agent })
    #expect(report.services.last?.kind == .loginItem)
    let status = Dictionary(uniqueKeysWithValues: report.services.map { ($0.name, $0.status) })
    #expect(status["com.filip.claude-acc.devguard.plist"] == .requiresApproval)
    #expect(status["com.filip.claude-acc.hotspot-user.plist"] == .notRegistered)
    #expect(status["com.filip.claude-acc.plist"] == .enabled)
    #expect(status["com.filip.claude-acc.menubar"] == .notFound)
    // no root helper offered: nothing in Pod registers pod-rootd yet
    #expect(report.daemons.isEmpty)
    #expect(report.needsApproval.map(\.name) == ["com.filip.claude-acc.devguard.plist"])
}

@Test("brew, turned off or not installed: no file, so Settings never shows a stale one")
func servicesReportOnlyForPod() async throws {
    for owner in [#"{"owner": "brew"}"#, #"{"owner": "none"}"#, nil, "not json"] {
        let home = try scratchHome(owner: owner)
        await Answers().report(home).refresh()
        #expect(try read(home) == nil, "owner \(owner ?? "missing")")
    }
}

@Test("unchanged answers are written again only after 15 minutes; a changed one at once")
func servicesReportRewrites() async throws {
    let home = try scratchHome(owner: podOwner)
    let answers = Answers()
    let report = answers.report(home)
    let start = Date(timeIntervalSince1970: 1_792_200_000)
    let file = AccPaths(home: home).podServices
    await report.refresh(now: start)
    #expect(FileManager.default.fileExists(atPath: file.path))

    try FileManager.default.removeItem(at: file)
    await report.refresh(now: start + 60)
    #expect(!FileManager.default.fileExists(atPath: file.path))

    answers.agents.withLock { $0["com.filip.claude-acc.janitor.plist"] = .requiresApproval }
    await report.refresh(now: start + 120)
    let changed = try #require(try read(home))
    #expect(changed.needsApproval.map(\.name) == ["com.filip.claude-acc.janitor.plist"])

    try FileManager.default.removeItem(at: file)
    await report.refresh(now: start + 120 + ServicesReport.rewriteEvery - 1)
    #expect(!FileManager.default.fileExists(atPath: file.path))
    await report.refresh(now: start + 120 + ServicesReport.rewriteEvery)
    #expect(try Date(try #require(try read(home)).at, strategy: .iso8601) == start + 120 + ServicesReport.rewriteEvery)
}

@Test("BTM's statuses map to AccKit's words")
func servicesStatusWords() {
    #expect(ServicesReport.word(.enabled) == "enabled")
    #expect(ServicesReport.word(.requiresApproval) == "requires-approval")
    #expect(ServicesReport.word(.notRegistered) == "not-registered")
    #expect(ServicesReport.word(.notFound) == "not-found")
}
