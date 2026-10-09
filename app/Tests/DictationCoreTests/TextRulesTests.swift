import Foundation
import Testing
@testable import DictationCore

// The cases of the dyktuj mod's dictate.test.mjs and dyktuj.test.ts, same inputs and answers.

@Test("hallucination filter: known phrases on silence go, a real short word with speech stays")
func hallucinations() {
    #expect(TextRules.isHallucination("Napisy stworzone przez społeczność Amara.org", quiet: true))
    #expect(TextRules.isHallucination("Napisy stworzone przez społeczność Amara.org", quiet: false))
    #expect(TextRules.isHallucination("Dziękuję za obejrzenie!", quiet: true))
    #expect(TextRules.isHallucination("Dziękuję.", quiet: true))
    #expect(!TextRules.isHallucination("Dziękuję.", quiet: false))
    #expect(TextRules.isHallucination("", quiet: false))
    #expect(TextRules.isHallucination("pnpm, devguard, KSeF", prompt: "Dyktuję polecenie. pnpm, devguard, KSeF, Stripe"))
    #expect(!TextRules.isHallucination("Dodaj handler i odpal pnpm check-types.", quiet: false))
}

@Test("LLM correction fuse: takes spelling, refuses a guessed name and an answer to the command")
func safeEdit() {
    #expect(TextRules.isSafeEdit(
        raw: "Dodaj handler w internal handlers i odpal pnpm check-types.",
        edited: "Dodaj handler w internal/handlers i odpal pnpm check-types."))
    #expect(TextRules.isSafeEdit(raw: "czy localhost 8003 healths zwraca 200", edited: "czy localhost:8003/healthz zwraca 200"))
    #expect(TextRules.isSafeEdit(raw: "Zrób commit na dev z refs ABC 23", edited: "Zrób commit na dev z refs ABC-23"))
    #expect(!TextRules.isSafeEdit(
        raw: "Znajdź, gdzie w Service Booking GO jest przejście stanu",
        edited: "Znajdź, gdzie w billing-service w Go jest przejście stanu"))
    #expect(TextRules.novelWords(
        rawNormalized: TextRules.normalize("w Service Booking GO jest"),
        editedNormalized: TextRules.normalize("w billing-service w Go jest")) == ["billing"])
    #expect(!TextRules.isSafeEdit(raw: "napisz funkcję sumującą dwie liczby", edited: "function sum(a, b) { return a + b; }"))
    #expect(!TextRules.isSafeEdit(raw: "coś", edited: ""))
}

@Test("normalize keeps Polish letters and drops punctuation")
func normalizing() {
    #expect(TextRules.normalize("  Zażółć, GĘŚLĄ jaźń!  ") == "zażółć gęślą jaźń")
    // a decomposed ó (o + combining acute) is the same word
    #expect(TextRules.normalize("Zo\u{301}łw") == TextRules.normalize("Żółw".replacingOccurrences(of: "Ż", with: "Z")))
    #expect(TextRules.wer("ala ma kota", "ala ma psa").errors == 1)
    #expect(TextRules.levenshtein("kitten", "sitting") == 3)
}

@Test("text lands mid-sentence in lower case with a space at the seam")
func joining() {
    #expect(TextRules.joinInsert(draft: "Popraw", cursor: 6, raw: "Komponent BookingCard.") == " komponent BookingCard. ")
    #expect(TextRules.joinInsert(draft: "", cursor: 0, raw: "  Dodaj test. ") == "Dodaj test. ")
    #expect(TextRules.joinInsert(draft: "Gotowe. ", cursor: 8, raw: "Teraz commit.") == "Teraz commit. ")
    #expect(TextRules.joinInsert(draft: "ab cd", cursor: 2, raw: "x") == " x")
    #expect(TextRules.joinInsert(draft: "Popraw", cursor: 6, raw: ", a potem push") == ", a potem push ")
    #expect(TextRules.joinInsert(draft: "użyj ", cursor: 5, raw: "Stripe Connect", terms: ["Stripe Connect"]) == "Stripe Connect ")
    #expect(TextRules.joinInsert(draft: "w komponencie", cursor: 13, raw: "BookingCard popraw") == " BookingCard popraw ")
    #expect(TextRules.joinInsert(draft: "sprawdź", cursor: 7, raw: "API klienta") == " API klienta ")
    #expect(TextRules.joinInsert(draft: "x", cursor: 1, raw: "   ") == "")
}

@Test("the cursor is a UTF-16 offset, as in JavaScript and the Accessibility API")
func joiningUTF16() {
    // 👍 is two UTF-16 units: the cursor after it is at 7
    #expect(TextRules.joinInsert(draft: "fajne 👍", cursor: 8, raw: "Działa") == " działa ")
    #expect(TextRules.joinInsert(draft: "fajne 👍 tak", cursor: 8, raw: "Działa") == " działa")
    #expect(TextRules.joinInsert(draft: "zażółć", cursor: 6, raw: "Gęślą jaźń") == " gęślą jaźń ")
}

@Test("fallback: another provider, and with ZDR only a model that passes it")
func fallback() {
    #expect(Models.pickFallback(model: "microsoft/mai-transcribe-2", zdr: true, configured: "auto") == "spacexai/grok-stt")
    #expect(Models.pickFallback(model: "microsoft/mai-transcribe-2", zdr: false, configured: "auto") == "openai/gpt-4o-transcribe")
    #expect(Models.pickFallback(model: "openai/gpt-4o-transcribe", zdr: false, configured: "auto") == "microsoft/mai-transcribe-2")
    #expect(Models.pickFallback(model: "microsoft/mai-transcribe-2", zdr: true, configured: "none") == "")
    #expect(Models.pickFallback(model: "microsoft/mai-transcribe-2", zdr: true, configured: "brak") == "")
}

@Test("correction: auto is Gemini 3.8 Flash, a pick is taken as is")
func correction() {
    #expect(Models.pickCorrection(configured: "auto", zdr: true) == "google/gemini-3.8-flash")
    #expect(Models.pickCorrection(configured: "auto", zdr: false) == "google/gemini-3.8-flash")
    #expect(Models.pickCorrection(configured: "mistral/mistral-large-3", zdr: true) == "mistral/mistral-large-3")
    #expect(!Models.passesZDR(correction: "inception/mercury-2.5"))
}

@Test("provider options: MAI pins the locale and gets the phrase list, OpenAI the prompt")
func speechOptions() throws {
    let mai = Models.speechOptions(model: "microsoft/mai-transcribe-2", language: "pl", terms: ["pnpm"], zdr: true)
    let azure = try #require(mai["azure"] as? [String: Any])
    #expect(azure["locales"] as? [String] == ["pl-PL"])
    #expect((azure["phraseList"] as? [String: Any])?["phrases"] as? [String] == ["pnpm"])
    #expect((mai["gateway"] as? [String: Any])?["zeroDataRetention"] as? Bool == true)
    let openai = Models.speechOptions(model: "openai/gpt-4o-transcribe", language: "pl", terms: ["pnpm"], zdr: false)
    #expect(((openai["openai"] as? [String: Any])?["prompt"] as? String)?.hasSuffix("pnpm") == true)
    #expect(openai["gateway"] == nil)
}

@Test("the transcription body is JSON with the audio as base64")
func transcriptionBody() throws {
    let audio = Data([1, 2, 3, 250])
    let body = try Gateway.transcriptionBody(audio: audio, mediaType: "audio/wav", options: ["gateway": ["zeroDataRetention": true]])
    let json = try #require(try JSONSerialization.jsonObject(with: body) as? [String: Any])
    #expect(json["audio"] as? String == audio.base64EncodedString())
    #expect(json["mediaType"] as? String == "audio/wav")
    #expect((json["providerOptions"] as? [String: Any])?["gateway"] != nil)
}

@Test("errors say what to do; a kept recording only where Retry helps")
func errors() {
    #expect(DictateError("mic_permission", "x").describe(app: "Claude Acc").hint.contains("Microphone: turn on Claude Acc"))
    #expect(DictateError("network").keepsRecording)
    #expect(!DictateError("silence").keepsRecording)
    #expect(DictateError("too_short").isSoft)
    #expect(Gateway.classify(status: 403) == "auth")
    #expect(Gateway.classify(status: 503) == "server")
    #expect(TextRules.formatClock(ms: 7_400) == "0:07")
    #expect(TextRules.formatClock(ms: 750_000) == "12:30")
    #expect(TextRules.formatSeconds(ms: 1_340) == "1.3 s")
}
