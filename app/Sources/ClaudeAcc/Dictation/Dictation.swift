import AppKit
import AVFoundation
import DictationCore
import Observation
import os

/// `log show --predicate 'subsystem == "com.filip.claude-acc"' --info`
let dictationLog = Logger(subsystem: "com.filip.claude-acc", category: "dictation")

/// Dictation in Polish into whatever app has the cursor: tap the right ⌘ to start and again to
/// insert, or hold it while you talk. The microphone runs in this process (AVAudioEngine), the transcript
/// comes from Vercel AI Gateway (MAI-Transcribe 2 with the repo's dictionary, then a Gemini
/// correction behind a safety fuse), and the text lands at the cursor. It replaces the dyktuj
/// mod of Claude Code: no Node, no ffmpeg, no process per recording.
@Observable
final class Dictation {
    enum Phase: Equatable {
        case idle, starting, recording, stopping, transcribing, done, error
    }

    struct Failure: Equatable {
        let code: String
        let title: String
        let hint: String
        let retryFile: URL?
        let isSoft: Bool
    }

    // MARK: State

    private(set) var phase = Phase.idle
    /// The last 100 ms levels in dBFS, newest last, for the wave.
    private(set) var levels: [Double] = []
    /// Milliseconds into the recording, 10 times a second: views read `elapsedSeconds`.
    @ObservationIgnored private(set) var elapsedMs = 0
    /// Whole seconds into the recording, changed once a second: the clocks read this.
    private(set) var elapsedSeconds = 0
    private(set) var handsFree = false
    private(set) var deviceName: String?
    /// A warning while recording: no sound yet, digital silence, the limit is near.
    private(set) var note: String?
    private(set) var activeModel: String?
    private(set) var attempt = 0
    private(set) var correcting = false
    private(set) var transcribingSince: Date?
    private(set) var failure: Failure?
    private(set) var lastText: String?
    private(set) var lastWords = 0
    private(set) var lastMs = 0
    private(set) var lastModel: String?
    /// The text waits on the clipboard: no Accessibility, or another app came to the front.
    private(set) var onClipboard = false
    private(set) var trusted = false
    private(set) var micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
    /// Accessibility was on and isn't any more: an app rebuilt with another signature.
    private(set) var lostTrust = false
    private(set) var testing = false
    private(set) var testResult: String?
    private(set) var devices: [InputDevice] = []

    var isActive: Bool { [.starting, .recording, .stopping].contains(phase) }

    // MARK: Settings

    var speechModel: String { didSet { save() } }
    /// "auto", "none" or a model.
    var fallbackModel: String { didSet { save() } }
    var zdr: Bool { didSet { save() } }
    /// "auto", "off" or a model.
    var correctionModel: String { didSet { save() } }
    var language: String { didSet { save() } }
    /// A device UID, or "" for the system input.
    var microphone: String { didSet { save() } }
    var preferBuiltIn: Bool { didSet { save() } }
    var maxMinutes: Int { didSet { save() } }
    /// "a, b, c" on top of the dictionary files.
    var extraTerms: String { didSet { save() } }
    var sounds: Bool { didSet { save() } }
    var widgetWhenIdle: Bool { didSet { save() } }
    /// Between the pieces of a long paste, so Claude Code doesn't glue them into one.
    var chunkGapMs: Int { didSet { save() } }

    // MARK: Internals

    static let directory = URL(fileURLWithPath: CLI.directory).appending(path: "dictation")
    static let retryDirectory = directory.appending(path: "retry")
    static let timingLog = directory.appending(path: "timing.log")
    static let userTerms = directory.appending(path: "slownik-user.txt")
    static let testRecording = directory.appending(path: "test.wav")

    @ObservationIgnored private let defaults = UserDefaults.standard
    @ObservationIgnored private let preview: Bool
    @ObservationIgnored private let recorder = Recorder()
    @ObservationIgnored let trigger = TriggerKey()
    @ObservationIgnored private var events: Task<Void, Never>?
    @ObservationIgnored private var work: Task<Void, Never>?
    @ObservationIgnored private var watchdog: Task<Void, Never>?
    @ObservationIgnored private var fade: Task<Void, Never>?
    @ObservationIgnored private var trustPoll: Task<Void, Never>?
    @ObservationIgnored private var stopWhenReady = false
    @ObservationIgnored private var targetPID: pid_t?
    @ObservationIgnored private var pressedAt: ContinuousClock.Instant?
    @ObservationIgnored private var firstBufferMs: Int?
    @ObservationIgnored private var stoppedAt: ContinuousClock.Instant?
    @ObservationIgnored private var key: String?
    @ObservationIgnored private var keyTask: Task<String?, Never>?
    @ObservationIgnored private var soundCache: [String: NSSound] = [:]

    init(preview: Bool = false) {
        self.preview = preview
        speechModel = defaults.string(forKey: "dictation.model") ?? Models.defaultSpeech
        fallbackModel = defaults.string(forKey: "dictation.fallback") ?? "auto"
        zdr = defaults.object(forKey: "dictation.zdr") as? Bool ?? true
        correctionModel = defaults.string(forKey: "dictation.correction") ?? "auto"
        language = defaults.string(forKey: "dictation.language") ?? "pl"
        microphone = defaults.string(forKey: "dictation.microphone") ?? ""
        preferBuiltIn = defaults.object(forKey: "dictation.preferBuiltIn") as? Bool ?? false
        maxMinutes = defaults.object(forKey: "dictation.maxMinutes") as? Int ?? 10
        extraTerms = defaults.string(forKey: "dictation.terms") ?? ""
        sounds = defaults.object(forKey: "dictation.sounds") as? Bool ?? true
        // off by default: the pill comes with dictation and goes with it
        widgetWhenIdle = defaults.object(forKey: "dictation.widgetIdle") as? Bool ?? false
        chunkGapMs = defaults.object(forKey: "dictation.chunkGapMs") as? Int ?? 300
        // a panel render shows the card as it looks once set up
        trusted = preview || AXIsProcessTrusted()
    }

    /// The tap, the trust watch and the sounds; after launch, never for a panel render.
    func activate() {
        guard !preview else { return }
        trigger.onStartHold = { [weak self] in self?.start(handsFree: false) }
        trigger.onStartHandsFree = { [weak self] in self?.start(handsFree: true) }
        trigger.onStop = { [weak self] in self?.stop() }
        trigger.onDiscard = { [weak self] in self?.cancel() }
        trigger.onEscape = { [weak self] in self?.cancel() }
        recorder.onFailure = { [weak self] error in self?.fail(error) }
        for name in ["start", "stop", "error"] {
            let path = Self.directory.appending(path: "sounds/\(name).wav").path
            soundCache[name] = NSSound(contentsOfFile: path, byReference: true)
        }
        cleanRetries()
        refreshTrust()
        devices = InputDevice.all()
        // the key now, off the main actor: the first dictation doesn't wait for it
        keyTask = Task { await Self.readKey() }
    }

    // MARK: Control

    /// From the widget: start hands free, or stop.
    func toggle() {
        switch phase {
        case .starting, .recording:
            stop()
        case .stopping, .transcribing:
            break
        case .idle, .done, .error:
            // without Accessibility the text could only go to the clipboard: ask first
            if !trusted { return requestTrust() }
            start(handsFree: true, fromCommand: true)
        }
    }

    /// The panel closes when dictation starts from its button: the text goes to the app behind it.
    static let startedFromPanel = Notification.Name("ClaudeAccDictationStarted")

    /// `fromCommand`: the widget, the panel's button or `claude-acc dictate`, not the key; a tap
    /// of the right ⌘ stops it.
    func start(handsFree: Bool, fromCommand: Bool = false) {
        guard [.idle, .done, .error].contains(phase), !preview else {
            // still busy with the last one: the key's hold or taps go nowhere
            if !isActive { trigger.setRecording(false) }
            return
        }
        fade?.cancel()
        discardRetryFile()
        failure = nil
        note = nil
        levels = []
        elapsedMs = 0
        elapsedSeconds = 0
        onClipboard = false
        stopWhenReady = false
        firstBufferMs = nil
        self.handsFree = handsFree
        phase = .starting
        dictationLog.notice("start, hands free: \(handsFree), from a command: \(fromCommand)")
        pressedAt = .now
        if fromCommand { trigger.enterHandsFree() } else { trigger.setRecording(true) }
        Gateway.prewarm()
        if key == nil, keyTask == nil { keyTask = Task { await Self.readKey() } }
        events = Task { [weak self] in await self?.record() }
    }

    private func record() async {
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            _ = await AVCaptureDevice.requestAccess(for: .audio)
        }
        micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
        guard phase == .starting else { return }
        guard micStatus == .authorized else {
            return fail(DictateError("mic_permission", "Claude Acc has no microphone permission"))
        }
        let stream: AsyncStream<RecorderEvent>
        do {
            stream = try recorder.start(
                deviceUID: microphone.isEmpty ? nil : microphone, preferBuiltIn: preferBuiltIn,
                maxSeconds: maxMinutes * 60)
        } catch {
            return fail((error as? DictateError) ?? DictateError("device", error.localizedDescription))
        }
        deviceName = recorder.deviceName
        startWatchdog()
        for await event in stream {
            switch event {
            case .firstBuffer:
                watchdog?.cancel()
                firstBufferMs = pressedAt.map { $0.duration(to: .now).milliseconds }
                note = nil
                phase = .recording
                play("start")
                if stopWhenReady { stop() }
            case .level(let ms, let db):
                elapsedMs = ms
                if ms / 1000 != elapsedSeconds { elapsedSeconds = ms / 1000 }
                // one assignment, one notification
                var next = levels.suffix(23)
                next.append(db)
                levels = Array(next)
                let left = maxMinutes * 60_000 - ms
                if maxMinutes > 1, left <= 30_000, note == nil {
                    note = "\(left / 1000) s to the length limit"
                }
            case .digitalSilence:
                note = "The microphone gives only zeros: Claude Acc probably has no permission"
            case .limit:
                stop()
            }
        }
    }

    /// No sound in 3 s: say so; none in 30 s: give up.
    private func startWatchdog() {
        watchdog?.cancel()
        watchdog = Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard let self, !Task.isCancelled, self.phase == .starting else { return }
            self.note = "Waiting for the microphone… if macOS asks, allow Claude Acc."
            try? await Task.sleep(for: .seconds(27))
            guard !Task.isCancelled, self.phase == .starting else { return }
            self.recorder.cancel()
            self.fail(DictateError("mic_timeout", "No sound from the microphone in 30 s"))
        }
    }

    func stop() {
        switch phase {
        case .starting:
            stopWhenReady = true
            return
        case .recording:
            break
        default:
            return
        }
        phase = .stopping
        dictationLog.notice("stop after \(self.elapsedMs) ms")
        trigger.setRecording(false)
        targetPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        play("stop")
        work = Task { [weak self] in
            // a short tail, so the last syllable isn't cut
            try? await Task.sleep(for: .milliseconds(250))
            guard let self, self.phase == .stopping else { return }
            self.stoppedAt = .now
            let (pcm, hitLimit) = self.recorder.finish()
            if hitLimit { self.note = "Reached the length limit: the recording stopped" }
            await self.transcribe(pcm, retryFile: nil)
        }
    }

    /// Escape, the widget's ×, or a diacritic typed while the hold had just started: no sound.
    func cancel() {
        dictationLog.notice("cancel in \(String(describing: self.phase), privacy: .public)")
        work?.cancel()
        events?.cancel()
        watchdog?.cancel()
        recorder.cancel()
        trigger.setRecording(false)
        if phase == .error {
            dismiss()
            return
        }
        phase = .idle
        note = nil
    }

    func retry() {
        guard let file = failure?.retryFile, [.error, .idle].contains(phase) else { return }
        guard let wav = try? Data(contentsOf: file) else {
            failure = nil
            phase = .idle
            return
        }
        failure = nil
        targetPID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        stoppedAt = .now
        firstBufferMs = nil
        phase = .transcribing
        work = Task { [weak self] in await self?.transcribe(Audio.wavToPcm(wav), retryFile: file) }
    }

    func dismiss() {
        fade?.cancel()
        discardRetryFile()
        failure = nil
        phase = .idle
    }

    func insertLast() {
        guard let text = lastText else { return }
        Task {
            let outcome = await Inserter.insert(text, terms: terms(), expectedPID: nil, gapMs: chunkGapMs)
            onClipboard = outcome == .clipboard
        }
    }

    // MARK: Transcription

    var pipelineOptions: Pipeline.Options {
        let correction = correctionModel == "off" ? "" : Models.pickCorrection(configured: correctionModel, zdr: zdr)
        return Pipeline.Options(
            model: speechModel, fallback: Models.pickFallback(model: speechModel, zdr: zdr, configured: fallbackModel),
            zdr: zdr, correction: correction, language: language, terms: terms())
    }

    func terms() -> [String] {
        Terms.load(files: [Self.directory.appending(path: "slownik.txt"), Self.userTerms], extra: Terms.parse(extraTerms))
    }

    private func transcribe(_ pcm: Data, retryFile: URL?) async {
        phase = .transcribing
        transcribingSince = .now
        activeModel = speechModel
        attempt = 1
        correcting = false
        let options = pipelineOptions
        guard let key = await apiKey() else {
            let file = retryFile ?? saveRetry(pcm)
            return fail(DictateError("no_key", "No AI_GATEWAY_API_KEY in the Keychain"), retryFile: file)
        }
        do {
            let result = try await Pipeline.run(pcm: pcm, options: options, service: Gateway(key: key)) { [weak self] event in
                Task { @MainActor in self?.apply(event) }
            }
            try Task.checkCancellation()
            guard phase == .transcribing else { return }
            let outcome = await Inserter.insert(result.text, terms: options.terms, expectedPID: targetPID, gapMs: chunkGapMs)
            if let retryFile { try? FileManager.default.removeItem(at: retryFile) }
            finished(result, outcome: outcome)
        } catch let error as DictateError {
            guard error.code != "cancelled", phase == .transcribing else { return }
            let file = error.keepsRecording ? retryFile ?? saveRetry(pcm) : nil
            if !error.keepsRecording, let retryFile { try? FileManager.default.removeItem(at: retryFile) }
            fail(error, retryFile: file)
        } catch {
            // cancelled
        }
    }

    private func apply(_ event: Pipeline.Event) {
        switch event {
        case .transcribing(let model, let attempt):
            activeModel = model
            self.attempt = attempt
        case .correcting:
            correcting = true
        case .correctionFailed(let message):
            note = "Correction failed, raw text: \(message)"
        }
    }

    private func finished(_ result: Pipeline.Result, outcome: Inserter.Outcome) {
        lastText = result.text
        lastWords = TextRules.countWords(result.text)
        lastMs = result.totalMs
        lastModel = result.model
        onClipboard = outcome == .clipboard
        phase = .done
        dictationLog.notice("inserted \(self.lastWords) words via \(String(describing: outcome), privacy: .public) in \(result.totalMs) ms")
        logTiming(result, outcome: outcome)
        fade?.cancel()
        fade = Task { [weak self] in
            try? await Task.sleep(for: .seconds(self?.onClipboard == true ? 6 : 1.5))
            guard let self, !Task.isCancelled, self.phase == .done else { return }
            self.phase = .idle
            self.onClipboard = false
        }
    }

    private func fail(_ error: DictateError, retryFile: URL? = nil) {
        dictationLog.error("failed: \(error.code, privacy: .public) \(error.message, privacy: .public)")
        watchdog?.cancel()
        trigger.setRecording(false)
        let (title, hint) = error.describe(app: "Claude Acc")
        failure = Failure(code: error.code, title: title, hint: hint, retryFile: retryFile, isSoft: error.isSoft)
        phase = .error
        note = nil
        fade?.cancel()
        if error.isSoft {
            // an empty try isn't a failure: the message goes by itself
            fade = Task { [weak self] in
                try? await Task.sleep(for: .seconds(5))
                guard let self, !Task.isCancelled, self.phase == .error, self.failure?.code == error.code else { return }
                self.dismiss()
            }
        } else {
            play("error")
        }
    }

    // MARK: Demo

    /// `ClaudeAcc --widget-demo`: the pill through every state with a made-up voice, to look
    /// at without the microphone. Only on a preview instance.
    func runDemo() async {
        guard preview else { return }
        trusted = true
        try? await Task.sleep(for: .seconds(0.8))
        phase = .starting
        try? await Task.sleep(for: .seconds(0.35))
        phase = .recording
        handsFree = true
        for step in 0..<40 {
            elapsedMs = step * 100
            elapsedSeconds = step / 10
            levels.append(-48 + 30 * abs(sin(Double(step) * 0.7)) + Double(step % 3) * 4)
            try? await Task.sleep(for: .milliseconds(100))
        }
        phase = .transcribing
        transcribingSince = .now
        activeModel = speechModel
        attempt = 1
        try? await Task.sleep(for: .seconds(1.2))
        correcting = true
        try? await Task.sleep(for: .seconds(0.6))
        lastWords = 14
        lastMs = 1_840
        phase = .done
        try? await Task.sleep(for: .seconds(1.5))
        phase = .idle
        try? await Task.sleep(for: .seconds(0.6))
    }

    // MARK: Test

    /// The shipped test recording through the whole path, without the microphone or inserting.
    func test() {
        guard !testing else { return }
        testing = true
        testResult = nil
        Task {
            defer { testing = false }
            guard let wav = try? Data(contentsOf: Self.testRecording) else {
                testResult = "No test recording at \(Self.testRecording.path)"
                return
            }
            guard let key = await apiKey() else {
                testResult = "No AI_GATEWAY_API_KEY in the Keychain"
                return
            }
            do {
                let r = try await Pipeline.run(pcm: Audio.wavToPcm(wav), options: pipelineOptions, service: Gateway(key: key))
                testResult = "\(TextRules.formatSeconds(ms: r.totalMs)) via \(Models.label(r.model)): \(r.text)"
            } catch let error as DictateError {
                testResult = "\(error.describe().title): \(error.message)"
            } catch {
                testResult = error.localizedDescription
            }
        }
    }

    // MARK: Permissions

    /// macOS shows its prompt only the first time: after that, the Settings pane itself.
    func requestTrust() {
        let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        trusted = AXIsProcessTrustedWithOptions(options)
        if !trusted { openPrivacy("Privacy_Accessibility") }
        refreshTrust()
    }

    func requestMicrophone() {
        if micStatus == .notDetermined {
            Task {
                _ = await AVCaptureDevice.requestAccess(for: .audio)
                micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
            }
        } else {
            openPrivacy("Privacy_Microphone")
        }
    }

    func openPrivacy(_ pane: String) {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") {
            NSWorkspace.shared.open(url)
        }
    }

    func refreshDevices() {
        devices = InputDevice.all()
        micStatus = AVCaptureDevice.authorizationStatus(for: .audio)
    }

    /// Until Accessibility is granted, look every 2 s; the tap goes in the moment it is.
    private func refreshTrust() {
        trusted = AXIsProcessTrusted()
        let had = defaults.bool(forKey: "dictation.wasTrusted")
        lostTrust = had && !trusted
        if trusted {
            defaults.set(true, forKey: "dictation.wasTrusted")
            trustPoll?.cancel()
            trustPoll = nil
            let installed = trigger.install()
            dictationLog.notice("accessibility granted, right ⌘ tap installed: \(installed), input monitoring: \(CGPreflightListenEventAccess())")
            return
        }
        dictationLog.notice("no accessibility yet (lost: \(self.lostTrust)); watching")
        guard trustPoll == nil else { return }
        trustPoll = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                guard let self else { return }
                if AXIsProcessTrusted() {
                    self.trustPoll = nil
                    self.refreshTrust()
                    return
                }
            }
        }
    }

    // MARK: Key, files, sounds

    private func apiKey() async -> String? {
        if let key { return key }
        let task = keyTask ?? Task { await Self.readKey() }
        keyTask = nil
        key = await task.value
        return key
    }

    /// The Gateway key from the Keychain through `security`: the item's access list already
    /// trusts that tool, so there is no prompt, and no "Always Allow" tied to this app's signature.
    @concurrent
    static func readKey() async -> String? {
        if let key = ProcessInfo.processInfo.environment["AI_GATEWAY_API_KEY"], !key.isEmpty { return key }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/security")
        process.arguments = ["find-generic-password", "-s", "AI_GATEWAY_API_KEY", "-w"]
        let result = CLI.runBlocking(process)
        let key = result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        return result.status == 0 && !key.isEmpty ? key : nil
    }

    private func saveRetry(_ pcm: Data) -> URL? {
        let stamp = Int(Date.now.timeIntervalSince1970 * 1000)
        let file = Self.retryDirectory.appending(path: "\(stamp).wav")
        do {
            try FileManager.default.createDirectory(at: Self.retryDirectory, withIntermediateDirectories: true)
            try Audio.pcmToWav(pcm).write(to: file, options: .atomic)
            return file
        } catch {
            return nil
        }
    }

    private func discardRetryFile() {
        if let file = failure?.retryFile { try? FileManager.default.removeItem(at: file) }
    }

    /// Recordings kept for Retry go after a day.
    private func cleanRetries() {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: Self.retryDirectory, includingPropertiesForKeys: [.contentModificationDateKey])
        else { return }
        for file in files {
            let date = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .now
            if date < .now.addingTimeInterval(-86_400) { try? fm.removeItem(at: file) }
        }
    }

    private func play(_ name: String) {
        guard sounds, let sound = soundCache[name] else { return }
        sound.stop()
        sound.play()
    }

    /// One line per dictation: key to first sound, stop to text, and the parts.
    private func logTiming(_ result: Pipeline.Result, outcome: Inserter.Outcome) {
        let stopToText = stoppedAt.map { $0.duration(to: .now).milliseconds } ?? -1
        let line = "\(Date.now.ISO8601Format()) seconds=\(result.seconds) first_buffer_ms=\(firstBufferMs ?? -1)"
            + " stop_to_text_ms=\(stopToText) stt_ms=\(result.sttMs) correction_ms=\(result.correctionMs ?? -1)"
            + " model=\(result.model) attempt=\(result.attempt) hedged=\(result.hedged) format=\(result.format)"
            + " insert=\(outcome)\n"
        let url = Self.timingLog
        Task.detached {
            let fm = FileManager.default
            try? fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(Data(line.utf8))
                try? handle.close()
            } else {
                try? Data(line.utf8).write(to: url)
            }
        }
    }

    private func save() {
        defaults.set(speechModel, forKey: "dictation.model")
        defaults.set(fallbackModel, forKey: "dictation.fallback")
        defaults.set(zdr, forKey: "dictation.zdr")
        defaults.set(correctionModel, forKey: "dictation.correction")
        defaults.set(language, forKey: "dictation.language")
        defaults.set(microphone, forKey: "dictation.microphone")
        defaults.set(preferBuiltIn, forKey: "dictation.preferBuiltIn")
        defaults.set(maxMinutes, forKey: "dictation.maxMinutes")
        defaults.set(extraTerms, forKey: "dictation.terms")
        defaults.set(sounds, forKey: "dictation.sounds")
        defaults.set(widgetWhenIdle, forKey: "dictation.widgetIdle")
        defaults.set(chunkGapMs, forKey: "dictation.chunkGapMs")
    }
}

extension Duration {
    var milliseconds: Int { Int(components.seconds * 1000 + components.attoseconds / 1_000_000_000_000_000) }
}
