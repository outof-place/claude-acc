import Foundation

/// From a finished recording to the text to insert: the checks before the network, the
/// transcription with its fallback, the hallucination filter and the LLM correction.
public enum Pipeline {
    public static let minSeconds = 0.5
    public static let minSpeechSeconds = 0.3

    public struct Options: Sendable, Equatable {
        public var model: String
        public var fallback: String
        public var zdr: Bool
        /// Empty: no correction.
        public var correction: String
        public var language: String
        public var terms: [String]
        /// When the fallback joins the race; nil: `Pipeline.hedgeDelay`.
        public var hedgeAfter: Double?

        public init(
            model: String, fallback: String, zdr: Bool, correction: String, language: String, terms: [String],
            hedgeAfter: Double? = nil
        ) {
            self.model = model
            self.fallback = fallback
            self.zdr = zdr
            self.correction = correction
            self.language = language
            self.terms = terms
            self.hedgeAfter = hedgeAfter
        }
    }

    /// When the fallback starts racing the main model. MAI-Transcribe 2 answers a few seconds
    /// of speech in 1.4-2.6 s, but now and then takes 6-25 s, with curl too; Grok STT, another
    /// provider, stayed at 1.1-1.9 s meanwhile (measured 2026-10-08, same recording, upload
    /// done in 0.2 s). Past this point both run and the first answer wins: MAI's spelling when
    /// it is quick, Grok's text with the correction's spelling when it isn't.
    public static func hedgeDelay(seconds: Double) -> Double { 3 + seconds * 0.1 }

    /// Errors no other model fixes: stop everything. ("zdr" is one model's: the other may pass.)
    static let fatal: Set<String> = ["auth", "cancelled", "no_key"]

    public struct Result: Sendable, Equatable {
        public let text: String
        /// The transcript before the correction, when the correction changed it.
        public let raw: String?
        public let model: String
        public let attempt: Int
        public let format: String
        public let sttMs: Int
        public let correctionMs: Int?
        public let totalMs: Int
        public let seconds: Double
        /// The fallback won the race against a slow main model.
        public var hedged = false
    }

    public enum Event: Sendable {
        case transcribing(model: String, attempt: Int)
        case correcting(model: String)
        case correctionFailed(String)
    }

    private struct HedgeTimer: Error {}

    /// The main model alone, then the fallback alongside it once the main one is slow or
    /// failed; the first transcript wins and the other request is cancelled.
    static func race(
        audio: Data, mediaType: String, seconds: Double, options: Options, service: some SpeechService,
        onEvent: @escaping @Sendable (Event) -> Void
    ) async throws -> (Transcript, hedged: Bool) {
        let main = options.model
        let fallback = options.fallback.isEmpty || options.fallback == main ? nil : options.fallback
        let delay = options.hedgeAfter ?? hedgeDelay(seconds: seconds)
        let request = { @Sendable (model: String) async throws -> Transcript in
            try await service.transcribe(
                audio, mediaType: mediaType, seconds: seconds, model: model, language: options.language,
                terms: options.terms, zdr: options.zdr, onAttempt: { onEvent(.transcribing(model: model, attempt: $0)) })
        }
        return try await withThrowingTaskGroup(of: Transcript.self) { group in
            group.addTask { try await request(main) }
            if fallback != nil {
                group.addTask {
                    try await Task.sleep(for: .seconds(delay))
                    throw HedgeTimer()
                }
            }
            var running = 1
            var fallbackStarted = false
            var firstError: DictateError?
            func startFallback() {
                guard let fallback, !fallbackStarted else { return }
                fallbackStarted = true
                running += 1
                group.addTask { try await request(fallback) }
            }
            while let result = await group.nextResult() {
                switch result {
                case .success(let transcript):
                    group.cancelAll()
                    return (transcript, transcript.model != main)
                case .failure(is HedgeTimer):
                    startFallback()
                case .failure(let error):
                    if error is CancellationError { continue }
                    let failure = error as? DictateError ?? DictateError("internal", error.localizedDescription)
                    running -= 1
                    if fatal.contains(failure.code) {
                        group.cancelAll()
                        throw failure
                    }
                    firstError = firstError ?? failure
                    // the main model gave up early: no point waiting for the timer
                    startFallback()
                    if running == 0 {
                        group.cancelAll()
                        throw firstError ?? failure
                    }
                }
            }
            throw firstError ?? DictateError("cancelled", "Cancelled")
        }
    }

    /// The recording is worth sending: not digital silence (no permission), not too short, with speech.
    public static func check(_ stats: Audio.SpeechStats) throws {
        if stats.isDigitalSilence, stats.seconds >= 1 {
            throw DictateError("mic_permission", "The microphone gave nothing but zeros (digital silence)")
        }
        if stats.seconds < minSeconds {
            throw DictateError("too_short", String(format: "The recording lasted %.1f s", stats.seconds))
        }
        if stats.speechSeconds < minSpeechSeconds {
            throw DictateError(
                "silence", "No speech found (noise \(stats.floorDb.map(String.init) ?? "?") dB, peak \(stats.peakDb.map(String.init) ?? "?") dB)")
        }
    }

    /// Off the main actor: base64, FLAC and the statistics of ten minutes of audio take a while.
    @concurrent
    public static func run(
        pcm: Data, options: Options, service: some SpeechService,
        onEvent: @escaping @Sendable (Event) -> Void = { _ in }
    ) async throws -> Result {
        let started = ContinuousClock.now
        let stats = Audio.speechStats(pcm)
        try check(stats)
        let flac = stats.seconds > 30 ? Audio.encodeFLAC(pcm) : nil
        let audio = flac ?? Audio.pcmToWav(pcm)
        let mediaType = flac == nil ? "audio/wav" : "audio/flac"

        let (transcript, hedged) = try await race(
            audio: audio, mediaType: mediaType, seconds: stats.seconds, options: options, service: service,
            onEvent: onEvent)

        let quiet = stats.speechSeconds < 1.0
        let prompt = transcript.model.hasPrefix("openai/") ? Models.biasPrompt(options.terms) : ""
        if TextRules.isHallucination(transcript.text, prompt: prompt, quiet: quiet) {
            throw DictateError("silence", "Dropped a result on silence: \"\(transcript.text.prefix(80))\"")
        }

        var text = transcript.text
        var raw: String?
        var correctionMs: Int?
        if !options.correction.isEmpty, !(options.zdr && !Models.passesZDR(correction: options.correction)) {
            onEvent(.correcting(model: options.correction))
            do {
                let edited = try await service.correct(text, model: options.correction, terms: options.terms, zdr: options.zdr)
                correctionMs = edited.ms
                if edited.accepted, edited.text != text {
                    raw = text
                    text = edited.text
                }
            } catch let error as DictateError {
                if error.code == "cancelled" { throw error }
                onEvent(.correctionFailed(error.message))
            }
        }
        try Task.checkCancellation()
        return Result(
            text: text, raw: raw, model: transcript.model, attempt: transcript.attempt, format: mediaType,
            sttMs: transcript.ms, correctionMs: correctionMs, totalMs: started.duration(to: .now).ms,
            seconds: (stats.seconds * 10).rounded() / 10, hedged: hedged)
    }
}
