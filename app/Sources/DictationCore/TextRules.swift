import Foundation

/// Text rules of dictation: the hallucination filter, the LLM correction's safety check and
/// quality measures, and how a transcript joins the text around the cursor. Ported one to one
/// from the dyktuj mod (stt.mjs, logic.ts); lengths count UTF-16 units like JavaScript and
/// like the Accessibility API's ranges.
public enum TextRules {
    // MARK: Normalizing and measuring

    /// Lower case, Polish letters kept, punctuation and symbols to spaces.
    public static func normalize(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        var lastWasSpace = true
        for scalar in text.precomposedStringWithCanonicalMapping.lowercased().unicodeScalars {
            if isLetterOrNumber(scalar) {
                out.append(scalar)
                lastWasSpace = false
            } else if !lastWasSpace {
                out.append(" ")
                lastWasSpace = true
            }
        }
        var result = String(out)
        if result.hasSuffix(" ") { result.removeLast() }
        return result
    }

    static func isLetterOrNumber(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.properties.generalCategory {
        case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter,
             .decimalNumber, .letterNumber, .otherNumber:
            return true
        default:
            return false
        }
    }

    /// Length as JavaScript counts it.
    static func length(_ text: String) -> Int { text.utf16.count }

    static func words(_ normalized: String) -> [String] {
        normalized.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
    }

    /// Edit distance over Unicode scalars.
    public static func levenshtein(_ a: String, _ b: String) -> Int {
        let a = Array(a.unicodeScalars), b = Array(b.unicodeScalars)
        var row = Array(0...b.count)
        guard !a.isEmpty else { return b.count }
        for i in 1...a.count {
            var diag = row[0]
            row[0] = i
            if b.isEmpty { continue }
            for j in 1...b.count {
                let tmp = row[j]
                row[j] = min(row[j] + 1, row[j - 1] + 1, diag + (a[i - 1] == b[j - 1] ? 0 : 1))
                diag = tmp
            }
        }
        return row[b.count]
    }

    public struct WER: Sendable, Equatable {
        public let errors: Int
        public let words: Int
        public let rate: Double
    }

    /// Word error rate of a hypothesis against a reference, both normalized.
    public static func wer(_ reference: String, _ hypothesis: String) -> WER {
        let r = words(normalize(reference)), h = words(normalize(hypothesis))
        var row = Array(0...h.count)
        if !r.isEmpty {
            for i in 1...r.count {
                var diag = row[0]
                row[0] = i
                if h.isEmpty { continue }
                for j in 1...h.count {
                    let tmp = row[j]
                    row[j] = min(row[j] + 1, row[j - 1] + 1, diag + (r[i - 1] == h[j - 1] ? 0 : 1))
                    diag = tmp
                }
            }
        }
        let errors = row[h.count]
        let rate = r.isEmpty ? (h.isEmpty ? 0 : 1) : Double(errors) / Double(r.count)
        return WER(errors: errors, words: r.count, rate: rate)
    }

    // MARK: LLM correction safety

    /// The LLM correction's fuse: a correction that changes too many words or the length, or
    /// brings a word with no cover in the raw transcript, is rejected whole and the raw text
    /// stays. Guards against a model that answers the dictated command instead of correcting
    /// it, or "guesses" another name from the repo (2026-10-03: "Service Booking GO" ->
    /// "billing-service w Go").
    public static func isSafeEdit(raw: String, edited: String) -> Bool {
        guard !edited.isEmpty else { return false }
        let a = normalize(raw), b = normalize(edited)
        guard !a.isEmpty else { return false }
        let ratio = Double(length(b)) / Double(length(a))
        if ratio < 0.7 || ratio > 1.35 { return false }
        if wer(a, b).rate > 0.35 { return false }
        return novelWords(rawNormalized: a, editedNormalized: b).isEmpty
    }

    /// Words of the correction with no cover in the raw text: not exact, not glued, not a small typo.
    public static func novelWords(rawNormalized: String, editedNormalized: String) -> [String] {
        let rawTokens = words(rawNormalized)
        let rawSet = Set(rawTokens)
        let joined = rawTokens.joined()
        var novel: [String] = []
        for t in words(editedNormalized) {
            if rawSet.contains(t) || length(t) <= 2 || t.allSatisfy(\.isASCIIDigit) || joined.contains(t) { continue }
            let budget = max(1, length(t) / 4)
            if rawTokens.contains(where: { abs(length($0) - length(t)) <= budget && levenshtein($0, t) <= budget }) {
                continue
            }
            novel.append(t)
        }
        return novel
    }

    // MARK: Hallucinations

    /// Known hallucinations of the models on silence (YouTube subtitles, thanks).
    public static let hallucinations = [
        "napisy stworzone przez społeczność amara.org",
        "napisy wykonane przez",
        "dziękuję za obejrzenie",
        "dziękuję za uwagę",
        "dzięki za obejrzenie",
        "zapraszam do subskrypcji",
        "subskrybuj",
        "subskrybujcie kanał",
        "thank you for watching",
        "thanks for watching",
        "thank you",
        "dziękuję",
        "do zobaczenia",
    ]
    private static let hallucinationSet = hallucinations.map(normalize)

    /// Whether to drop a result: empty, a known hallucination or an echo of the prompt. Short
    /// phrases ("dziękuję") are dropped only when the recording was quiet, since a person may
    /// really dictate them.
    public static func isHallucination(_ text: String, prompt: String = "", quiet: Bool = false) -> Bool {
        let n = normalize(text)
        if n.isEmpty { return true }
        let p = normalize(prompt)
        if !p.isEmpty, length(n) > 8, p.contains(n) { return true }
        if !quiet {
            return hallucinationSet.contains { length($0) > 20 && n.hasPrefix($0) && length(n) - length($0) < 4 }
        }
        if hallucinationSet.contains(n) { return true }
        return hallucinationSet.contains { n.hasPrefix($0) && length(n) - length($0) < 4 }
    }

    // MARK: Joining the cursor's text

    private static let closers = Set(".,;:!?)]}…".unicodeScalars)
    private static let openers = Set("([{\"'„«/".unicodeScalars)

    /// The text to insert at the cursor: a space at the seam where there is none, and a lower
    /// case first letter when dictation lands mid-sentence (but not in a name the dictionary
    /// writes with a capital: "Stripe").
    ///
    /// `before` and `after` are the text around the cursor; a window of it is enough.
    public static func joinInsert(before: String, after: String, raw: String, terms: [String] = []) -> String {
        var text = raw.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if text.isEmpty { return "" }
        var prev = Substring(before)
        while let last = prev.last, last == " " || last == "\t" { prev.removeLast() }
        let midSentence = !prev.isEmpty && !(prev.last.map { ".!?\n".contains($0) } ?? false)
        if midSentence {
            let firstWord = text.split(
                maxSplits: 1, omittingEmptySubsequences: false,
                whereSeparator: { $0.isWhitespace || ",.;:!?".contains($0) }
            ).first.map(String.init) ?? ""
            let isTerm = terms.contains { $0.split(separator: " ").first.map(String.init) == firstWord }
            if isPlainCapital(firstWord), !isTerm, let first = text.first {
                text = first.lowercased() + text.dropFirst()
            }
        }
        let firstScalar = text.unicodeScalars.first
        let needsLead = !before.isEmpty
            && !(before.last?.isWhitespace ?? false)
            && !(before.unicodeScalars.last.map(openers.contains) ?? false)
            && !(firstScalar.map(closers.contains) ?? false)
        let needsTrail = after.isEmpty
            || (!(after.first?.isWhitespace ?? false) && !(after.unicodeScalars.first.map(closers.contains) ?? false))
        return (needsLead ? " " : "") + text + (needsTrail ? " " : "")
    }

    /// The same for a whole draft and a cursor at a UTF-16 offset.
    public static func joinInsert(draft: String, cursor: Int, raw: String, terms: [String] = []) -> String {
        let ns = draft as NSString
        let at = min(max(0, cursor), ns.length)
        return joinInsert(before: ns.substring(to: at), after: ns.substring(from: at), raw: raw, terms: terms)
    }

    /// Only a plain capitalized word ("Który"); "BookingCard", "API" and dictionary terms stay.
    static func isPlainCapital(_ word: String) -> Bool {
        let scalars = Array(word.unicodeScalars)
        guard scalars.count >= 2, scalars[0].properties.generalCategory == .uppercaseLetter else { return false }
        return scalars.dropFirst().allSatisfy { $0.properties.generalCategory == .lowercaseLetter }
    }

    // MARK: Small formats

    public static func countWords(_ text: String) -> Int {
        text.split(whereSeparator: { $0.isWhitespace }).count
    }

    /// 0:07, 12:30.
    public static func formatClock(ms: Int) -> String {
        let s = max(0, ms / 1000)
        return "\(s / 60):" + (s % 60 < 10 ? "0" : "") + "\(s % 60)"
    }

    /// 1.3 s.
    public static func formatSeconds(ms: Int) -> String {
        String(format: "%.1f s", Double(ms) / 1000)
    }
}

private extension Character {
    var isASCIIDigit: Bool { isASCII && isNumber }
}
