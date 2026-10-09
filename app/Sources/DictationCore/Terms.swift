import Foundation

/// The dictation dictionary: names whose spelling the model must hit, one per line.
public enum Terms {
    /// The shipped dictionary, then the person's own file (it survives updates), then the terms
    /// from the settings; each term once, whatever its case.
    public static func load(files: [URL], extra: [String] = []) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        func add(_ raw: Substring) {
            let term = raw.trimmingCharacters(in: .whitespaces)
            guard !term.isEmpty, !term.hasPrefix("#"), seen.insert(term.lowercased()).inserted else { return }
            out.append(term)
        }
        for file in files {
            guard let text = try? String(contentsOf: file, encoding: .utf8) else { continue }
            for line in text.split(whereSeparator: \.isNewline) { add(line) }
        }
        for term in extra { add(Substring(term)) }
        return out
    }

    /// "a, b, c" from the settings field.
    public static func parse(_ field: String) -> [String] {
        field.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }
}

/// Text for a terminal, in pastes Claude Code shows as they are: it folds a paste of more than
/// 800 characters or 3 lines into "[Pasted text #N]".
public enum Chunker {
    public static let limit = 780

    /// Pieces of at most `limit` UTF-16 units, cut after a space; pasted one after another they
    /// give back the whole text. Line breaks become spaces: outside a bracketed paste a newline
    /// is Enter and sends the prompt half done.
    public static func chunks(_ text: String, limit: Int = limit) -> [String] {
        let flat = text.replacingOccurrences(of: "\r\n|[\r\n\u{2028}\u{2029}]", with: " ", options: .regularExpression)
        guard flat.utf16.count > limit else { return flat.isEmpty ? [] : [flat] }
        var pieces: [String] = []
        var current = ""
        var currentLength = 0
        // words with the space after them, so the pieces join back exactly
        var word = ""
        func flushWord() {
            let length = word.utf16.count
            if currentLength + length > limit, !current.isEmpty {
                pieces.append(current)
                current = ""
                currentLength = 0
            }
            if length > limit {
                // one word longer than a piece: cut it hard, on Character boundaries
                for character in word {
                    let size = String(character).utf16.count
                    if currentLength + size > limit {
                        pieces.append(current)
                        current = ""
                        currentLength = 0
                    }
                    current.append(character)
                    currentLength += size
                }
            } else {
                current += word
                currentLength += length
            }
            word = ""
        }
        for character in flat {
            word.append(character)
            if character == " " { flushWord() }
        }
        if !word.isEmpty { flushWord() }
        if !current.isEmpty { pieces.append(current) }
        return pieces
    }
}
