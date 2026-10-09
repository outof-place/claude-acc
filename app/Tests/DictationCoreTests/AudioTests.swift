import Foundation
import Testing
@testable import DictationCore

/// The test recording: macOS `say -v Zosia` (16 kHz mono via afconvert) saying "Sprawdź, czy nowy
/// serwer jest zarejestrowany w devguard, bo inaczej mamy wyciek pamięci między sesjami."
func fixture() throws -> Data {
    let url = try #require(Bundle.module.url(forResource: "devguard", withExtension: "wav", subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

/// Seconds of a sine at `db` dBFS, or of noise when `noise`.
func tone(seconds: Double, db: Double, noise: Bool = false) -> Data {
    let count = Int(seconds * Double(Audio.sampleRate))
    let amplitude = 32767 * pow(10, db / 20)
    var samples = [Int16](repeating: 0, count: count)
    var rng = SystemRandomNumberGenerator()
    for i in 0..<count {
        let x = noise ? Double.random(in: -1...1, using: &rng) : sin(Double(i) * 2 * .pi * 220 / Double(Audio.sampleRate))
        samples[i] = Int16(amplitude * x)
    }
    return samples.withUnsafeBufferPointer { Data(buffer: $0) }
}

@Test("speech detection: digital silence, noise and speech")
func speechDetection() throws {
    let zeros = Audio.speechStats(Data(count: 32_000 * 2))
    #expect(zeros.isDigitalSilence)
    #expect(zeros.speechSeconds == 0)
    let speech = Audio.speechStats(Audio.wavToPcm(try fixture()))
    #expect(speech.speechSeconds > 3, "speech \(speech.speechSeconds) s")
    #expect(!speech.isDigitalSilence)
    let hiss = Audio.speechStats(tone(seconds: 2, db: -45, noise: true))
    #expect(hiss.speechSeconds < Pipeline.minSpeechSeconds)
    #expect(Audio.pcmToWav(Data(count: 10)).count == 54)
}

@Test("WAV round trip, also through a header longer than 44 bytes")
func wavRoundTrip() throws {
    let pcm = tone(seconds: 0.2, db: -12)
    #expect(Audio.wavToPcm(Audio.pcmToWav(pcm)) == pcm)
    #expect(Audio.wavToPcm(try fixture()).count == 218_916)
}

@Test("a recording over 30 s goes up as FLAC: smaller, with its samples in the header")
func flac() throws {
    let pcm = Audio.wavToPcm(try fixture())
    var long = Data()
    for _ in 0..<6 { long.append(pcm) }
    let encoded = try #require(Audio.encodeFLAC(long))
    #expect(encoded.prefix(4) == Data("fLaC".utf8))
    #expect(encoded.count < long.count * 3 / 4, "FLAC \(encoded.count) of \(long.count) bytes")
    // STREAMINFO: the 36-bit total sample count sits in bytes 21...26 of the file
    let info = [UInt8](encoded[18..<26])
    let total = (UInt64(info[3] & 0x0F) << 32) | UInt64(info[4]) << 24 | UInt64(info[5]) << 16 | UInt64(info[6]) << 8
        | UInt64(info[7])
    #expect(total == UInt64(long.count / 2))
}

@Test("chunks stay under Claude Code's paste fold and join back exactly")
func chunks() {
    #expect(Chunker.chunks("") == [])
    #expect(Chunker.chunks("krótko") == ["krótko"])
    #expect(Chunker.chunks("dwie\nlinie\r\ni\rjeszcze") == ["dwie linie i jeszcze"])
    let sentence = "Sprawdź, czy nowy serwer jest zarejestrowany w devguard, bo inaczej mamy wyciek pamięci między sesjami. "
    let long = String(repeating: sentence, count: 30)
    let pieces = Chunker.chunks(long)
    #expect(pieces.count > 3)
    #expect(pieces.allSatisfy { $0.utf16.count <= Chunker.limit })
    #expect(pieces.joined() == long)
    #expect(pieces.dropLast().allSatisfy { $0.hasSuffix(" ") })
    let word = String(repeating: "ą", count: 2000)
    let hard = Chunker.chunks(word)
    #expect(hard.joined() == word)
    #expect(hard.allSatisfy { $0.utf16.count <= Chunker.limit })
}

@Test("terms: shipped file, the person's file, settings; each once whatever its case")
func terms() throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "terms-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    try "# komentarz\npnpm\nStripe\n\n".write(to: dir.appending(path: "a.txt"), atomically: true, encoding: .utf8)
    try "stripe\nKSeF\n".write(to: dir.appending(path: "b.txt"), atomically: true, encoding: .utf8)
    let loaded = Terms.load(
        files: [dir.appending(path: "a.txt"), dir.appending(path: "b.txt"), dir.appending(path: "missing.txt")],
        extra: Terms.parse(" Orca , pnpm,"))
    #expect(loaded == ["pnpm", "Stripe", "KSeF", "Orca"])
}
