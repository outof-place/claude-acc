import Foundation
import Testing
@testable import DictationCore

// MARK: The dictation key (right ⌘)

@Test("shortcuts with the key (⌘C, ⌘V) never start the microphone")
func diacritics() {
    var m = TriggerMachine()
    // ⌘ down, C down, C up, ⌘ up: typed fast
    #expect(m.keyDown(at: 0, otherModifiers: false) == nil)
    #expect(m.otherInput(at: 0.08, isKey: true) == nil)
    #expect(m.tick(at: 0.4) == nil)
    #expect(m.keyUp(at: 0.15) == nil)
    // ⌘ held slowly before the letter: the hold starts, the key throws it away
    #expect(m.keyDown(at: 1, otherModifiers: false) == nil)
    #expect(m.tick(at: 1.3) == .startHold)
    #expect(m.otherInput(at: 1.5, isKey: true) == .discard)
    #expect(m.keyUp(at: 1.6) == nil)
    // two quick shortcuts in a row
    #expect(m.keyDown(at: 2, otherModifiers: false) == nil)
    #expect(m.otherInput(at: 2.05, isKey: true) == nil)
    #expect(m.keyUp(at: 2.1) == nil)
    #expect(m.keyDown(at: 2.2, otherModifiers: false) == nil)
    #expect(m.otherInput(at: 2.25, isKey: true) == nil)
    #expect(m.keyUp(at: 2.3) == nil)
    #expect(!m.isRecording)
}

@Test("hold to talk: starts after 300 ms, stops on release; a click while held is fine once recording")
func holdToTalk() {
    var m = TriggerMachine()
    _ = m.keyDown(at: 10, otherModifiers: false)
    #expect(m.holdDeadline == 10.3)
    #expect(m.tick(at: 10.2) == nil)
    #expect(m.tick(at: 10.3) == .startHold)
    #expect(m.isHolding)
    #expect(m.otherInput(at: 10.5, isKey: false) == nil)
    #expect(m.otherInput(at: 12, isKey: true) == nil)
    #expect(m.keyUp(at: 15) == .stop)
    #expect(!m.isRecording)
}

@Test("⌘-click and ⌘⇧ shortcuts don't count")
func modifiers() {
    var m = TriggerMachine()
    _ = m.keyDown(at: 0, otherModifiers: true)
    #expect(m.tick(at: 1) == nil)
    #expect(m.keyUp(at: 1.1) == nil)
    _ = m.keyDown(at: 2, otherModifiers: false)
    _ = m.otherInput(at: 2.1, isKey: false)
    #expect(m.tick(at: 2.5) == nil)
    #expect(m.keyUp(at: 2.6) == nil)
    #expect(!m.isRecording)
}

@Test("a tap starts like a button, the next tap stops; typing in between doesn't")
func tapToggles() {
    var m = TriggerMachine()
    _ = m.keyDown(at: 0, otherModifiers: false)
    #expect(m.keyUp(at: 0.1) == .startHandsFree)
    #expect(m.isRecording)
    // a shortcut during the recording doesn't stop it
    _ = m.keyDown(at: 5, otherModifiers: false)
    _ = m.otherInput(at: 5.05, isKey: true)
    #expect(m.keyUp(at: 5.1) == nil)
    #expect(m.isRecording)
    _ = m.keyDown(at: 9, otherModifiers: false)
    #expect(m.keyUp(at: 9.1) == .stop)
    #expect(!m.isRecording)
}

@Test("a press between a tap and a hold is nothing")
func inBetween() {
    var m = TriggerMachine()
    _ = m.keyDown(at: 3, otherModifiers: false)
    #expect(m.keyUp(at: 3.28) == nil)
    #expect(!m.isRecording)
}

// MARK: From recording to text

/// The network as the tests want it: a script of answers per model and a log of the calls.
final class FakeService: SpeechService, @unchecked Sendable {
    var transcripts: [String: Result<String, DictateError>] = [:]
    /// Seconds a model takes to answer.
    var delays: [String: Double] = [:]
    var correction: Result<String, DictateError>?
    private(set) var calls: [String] = []
    private let lock = NSLock()

    func transcribe(
        _ audio: Data, mediaType: String, seconds: Double, model: String, language: String, terms: [String],
        zdr: Bool, onAttempt: @escaping @Sendable (Int) -> Void
    ) async throws -> Transcript {
        lock.withLock { calls.append("stt \(model) \(mediaType)") }
        onAttempt(1)
        if let delay = delays[model] { try await Task.sleep(for: .seconds(delay)) }
        switch transcripts[model] ?? .failure(DictateError("server", "no script")) {
        case .success(let text): return Transcript(text: text, model: model, attempt: 1, ms: 5)
        case .failure(let error): throw error
        }
    }

    func correct(_ text: String, model: String, terms: [String], zdr: Bool) async throws -> Correction {
        lock.withLock { calls.append("fix \(model)") }
        switch correction ?? .success(text) {
        case .success(let edited): return Correction(text: edited, ms: 3, accepted: TextRules.isSafeEdit(raw: text, edited: edited))
        case .failure(let error): throw error
        }
    }
}

let options = Pipeline.Options(
    model: "microsoft/mai-transcribe-2", fallback: "spacexai/grok-stt", zdr: true, correction: "google/gemini-3.8-flash",
    language: "pl", terms: ["devguard"])

@Test("a slow main model gets the fallback alongside it, and the faster answer wins")
func hedgeAgainstFallback() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .success("Wolna odpowiedź.")
    service.transcripts["spacexai/grok-stt"] = .success("Szybka odpowiedź.")
    service.delays["microsoft/mai-transcribe-2"] = 3
    service.delays["spacexai/grok-stt"] = 0.05
    var racing = options
    racing.hedgeAfter = 0.2
    racing.correction = ""
    let started = ContinuousClock.now
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: racing, service: service)
    #expect(result.text == "Szybka odpowiedź.")
    #expect(result.model == "spacexai/grok-stt")
    #expect(result.hedged)
    #expect(started.duration(to: .now) < .seconds(1.5))
}

@Test("a quick main model runs alone")
func noHedge() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .success("Szybko.")
    service.delays["microsoft/mai-transcribe-2"] = 0.05
    var racing = options
    racing.hedgeAfter = 0.5
    racing.correction = ""
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: racing, service: service)
    #expect(!result.hedged)
    try await Task.sleep(for: .seconds(0.7))
    #expect(service.calls == ["stt microsoft/mai-transcribe-2 audio/wav"])
}

@Test("a main model that fails early starts the fallback at once, not at the hedge time")
func earlyFailure() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .failure(DictateError("zdr", "HTTP 400: no ZDR"))
    service.transcripts["spacexai/grok-stt"] = .success("Zapas.")
    var racing = options
    racing.hedgeAfter = 10
    racing.correction = ""
    let started = ContinuousClock.now
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: racing, service: service)
    #expect(result.model == "spacexai/grok-stt")
    #expect(started.duration(to: .now) < .seconds(1))
}

@Test("a recording goes to the main model, the correction takes spelling only")
func happyPath() async throws {
    let service = FakeService()
    let raw = "Sprawdź, czy nowy serwer jest zarejestrowany w dev guard, bo inaczej mamy wyciek pamięci."
    let fixed = "Sprawdź, czy nowy serwer jest zarejestrowany w devguard, bo inaczej mamy wyciek pamięci."
    service.transcripts["microsoft/mai-transcribe-2"] = .success(raw)
    service.correction = .success(fixed)
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: options, service: service)
    #expect(result.text == fixed)
    #expect(result.raw == raw)
    #expect(result.format == "audio/wav")
    #expect(service.calls == ["stt microsoft/mai-transcribe-2 audio/wav", "fix google/gemini-3.8-flash"])
}

@Test("a correction that answers the command is refused and the raw text stays")
func refusedCorrection() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .success("napisz funkcję sumującą dwie liczby")
    service.correction = .success("function sum(a, b) { return a + b; }")
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: options, service: service)
    #expect(result.text == "napisz funkcję sumującą dwie liczby")
    #expect(result.raw == nil)
}

@Test("the fallback takes over when the main model fails; a refused key stops at once")
func fallbackTakesOver() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .failure(DictateError("server", "HTTP 500"))
    service.transcripts["spacexai/grok-stt"] = .success("Działa.")
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: options, service: service)
    #expect(result.model == "spacexai/grok-stt")

    let refused = FakeService()
    refused.transcripts["microsoft/mai-transcribe-2"] = .failure(DictateError("auth", "HTTP 401"))
    await #expect(throws: DictateError("auth", "HTTP 401", status: nil)) {
        try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: options, service: refused)
    }
    #expect(refused.calls.count == 1)
}

@Test("digital silence is a missing permission, noise is silence; neither reaches the network")
func nothingToSend() async throws {
    let service = FakeService()
    await #expect(throws: DictateError.self) {
        try await Pipeline.run(pcm: Data(count: 32_000 * 2), options: options, service: service)
    }
    do {
        _ = try await Pipeline.run(pcm: tone(seconds: 2, db: -45, noise: true), options: options, service: service)
        Issue.record("noise went through")
    } catch let error as DictateError {
        #expect(error.code == "silence")
        #expect(!error.keepsRecording)
    }
    do {
        _ = try await Pipeline.run(pcm: tone(seconds: 0.3, db: -12), options: options, service: service)
    } catch let error as DictateError {
        #expect(error.code == "too_short")
    }
    #expect(service.calls.isEmpty)
}

@Test("a hallucination on a quiet recording is dropped")
func hallucinationDropped() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .success("Dziękuję za obejrzenie!")
    var quiet = Data(count: 16_000 * 2)
    quiet.append(tone(seconds: 0.5, db: -20))
    do {
        _ = try await Pipeline.run(pcm: quiet, options: options, service: service)
        Issue.record("hallucination went through")
    } catch let error as DictateError {
        #expect(error.code == "silence")
    }
}

@Test("a network error keeps the recording for Retry")
func networkError() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .failure(DictateError("network", "offline"))
    service.transcripts["spacexai/grok-stt"] = .failure(DictateError("network", "offline"))
    do {
        _ = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: options, service: service)
        Issue.record("went through offline")
    } catch let error as DictateError {
        #expect(error.code == "network")
        #expect(error.keepsRecording)
    }
}

@Test("a ZDR run skips a correction model without ZDR")
func correctionWithoutZDR() async throws {
    let service = FakeService()
    service.transcripts["microsoft/mai-transcribe-2"] = .success("Działa.")
    var noZDR = options
    noZDR.correction = "inception/mercury-2.5"
    _ = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: noZDR, service: service)
    #expect(service.calls == ["stt microsoft/mai-transcribe-2 audio/wav"])
}

// MARK: The real Gateway (DICTATION_LIVE=1)

let live = ProcessInfo.processInfo.environment["DICTATION_LIVE"] == "1"

/// The live runs use the shipped dictionary, as the app does.
let liveOptions: Pipeline.Options = {
    let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appending(path: "../../../dictation/slownik.txt")
    var o = options
    o.terms = Terms.load(files: [repo.standardizedFileURL])
    return o
}()

func liveKey() throws -> String {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
    process.arguments = ["find-generic-password", "-s", "AI_GATEWAY_API_KEY", "-w"]
    let out = Pipe()
    process.standardOutput = out
    try process.run()
    process.waitUntilExit()
    return String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        .trimmingCharacters(in: .whitespacesAndNewlines)
}

@Test("the default model with ZDR hits the dictionary's terms", .enabled(if: live))
func liveTranscription() async throws {
    let gateway = Gateway(key: try liveKey())
    var plain = liveOptions
    plain.correction = ""
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: plain, service: gateway)
    #expect(result.text.contains("devguard"), "\(result.text)")
    #expect(result.text.contains("pamięci"), "\(result.text)")
    #expect(result.format == "audio/wav")
    #expect(liveOptions.terms.count > 100)
    print("live stt \(result.sttMs) ms: \(result.text)")
}

@Test("the correction through the Gateway with ZDR keeps the terms", .enabled(if: live))
func liveCorrection() async throws {
    let gateway = Gateway(key: try liveKey())
    for model in ["google/gemini-3.8-flash", "mistral/mistral-large-3"] {
        var fixing = liveOptions
        fixing.correction = model
        let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: fixing, service: gateway)
        #expect(result.text.contains("devguard"), "\(model): \(result.text)")
        #expect(result.correctionMs != nil)
        print("live \(model): stt \(result.sttMs) ms, correction \(result.correctionMs ?? -1) ms, total \(result.totalMs) ms: \(result.text)")
    }
}

@Test("the fallback takes over from a model that doesn't exist", .enabled(if: live))
func liveFallback() async throws {
    let gateway = Gateway(key: try liveKey())
    var broken = liveOptions
    broken.model = "microsoft/no-such-model"
    broken.fallback = "microsoft/mai-transcribe-2"
    broken.correction = ""
    let result = try await Pipeline.run(pcm: Audio.wavToPcm(try fixture()), options: broken, service: gateway)
    #expect(result.model == "microsoft/mai-transcribe-2")
}

@Test("over 30 s goes up as FLAC", .enabled(if: live))
func liveFLAC() async throws {
    let gateway = Gateway(key: try liveKey())
    let pcm = Audio.wavToPcm(try fixture())
    var long = Data()
    for _ in 0..<6 { long.append(pcm) }
    var plain = liveOptions
    plain.correction = ""
    let result = try await Pipeline.run(pcm: long, options: plain, service: gateway)
    #expect(result.format == "audio/flac")
    // without the correction the STT alone may write the term with a space on a long upload
    let joined = result.text.lowercased().replacingOccurrences(of: " ", with: "")
    #expect(joined.contains("devguard"), "\(result.text)")
}
