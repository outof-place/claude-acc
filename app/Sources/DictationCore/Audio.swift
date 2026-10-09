import Accelerate
import AVFAudio
import Foundation

/// Audio of dictation: WAV, loudness and speech detection on raw PCM, 16 kHz mono 16 bit.
public enum Audio {
    public static let sampleRate = 16_000
    /// 100 ms.
    public static let windowSamples = 1_600
    public static let bytesPerSecond = sampleRate * 2

    // MARK: WAV

    /// A WAV header for 16-bit mono PCM in front of the data.
    public static func pcmToWav(_ pcm: Data, sampleRate: Int = sampleRate) -> Data {
        var header = Data(capacity: 44 + pcm.count)
        func u32(_ v: Int) { withUnsafeBytes(of: UInt32(v).littleEndian) { header.append(contentsOf: $0) } }
        func u16(_ v: Int) { withUnsafeBytes(of: UInt16(v).littleEndian) { header.append(contentsOf: $0) } }
        header.append(contentsOf: Array("RIFF".utf8))
        u32(36 + pcm.count)
        header.append(contentsOf: Array("WAVEfmt ".utf8))
        u32(16)
        u16(1)
        u16(1)
        u32(sampleRate)
        u32(sampleRate * 2)
        u16(2)
        u16(16)
        header.append(contentsOf: Array("data".utf8))
        u32(pcm.count)
        header.append(pcm)
        return header
    }

    /// The PCM of a WAV: looks for the "data" chunk, so headers longer than 44 bytes work.
    public static func wavToPcm(_ wav: Data) -> Data {
        let bytes = [UInt8](wav)
        var offset = 12
        while offset + 8 <= bytes.count {
            let id = String(decoding: bytes[offset..<offset + 4], as: UTF8.self)
            let size = Int(bytes[offset + 4]) | Int(bytes[offset + 5]) << 8 | Int(bytes[offset + 6]) << 16
                | Int(bytes[offset + 7]) << 24
            if id == "data" {
                return wav.subdata(in: offset + 8..<min(bytes.count, offset + 8 + size))
            }
            offset += 8 + size + (size % 2)
        }
        return bytes.count > 44 ? wav.subdata(in: 44..<bytes.count) : Data()
    }

    // MARK: Levels

    public struct Level: Sendable, Equatable {
        /// RMS in dBFS; digital silence is -infinity.
        public let db: Double
        /// Absolute peak, 0...32768.
        public let peak: Int
    }

    /// RMS and peak of a run of samples.
    public static func level(_ samples: UnsafeBufferPointer<Int16>, scratch: inout [Float]) -> Level {
        let n = samples.count
        guard n > 0 else { return Level(db: -.infinity, peak: 0) }
        if scratch.count < n { scratch = [Float](repeating: 0, count: n) }
        scratch.withUnsafeMutableBufferPointer { floats in
            vDSP.convertElements(of: samples, to: &floats[0..<n])
        }
        let (rms, peak) = scratch.withUnsafeBufferPointer { floats -> (Float, Float) in
            let window = UnsafeBufferPointer(rebasing: floats[0..<n])
            return (vDSP.rootMeanSquare(window), vDSP.maximumMagnitude(window))
        }
        let db = rms > 0 ? 20 * log10(Double(rms) / 32768) : -.infinity
        return Level(db: db, peak: Int(peak))
    }

    /// Statistics of speech over a whole recording: the noise floor (10th percentile of the
    /// windows) and the windows clearly above it. The threshold is relative, because a quiet
    /// mic and a loud room give entirely different absolute values.
    public struct SpeechStats: Sendable, Equatable {
        public let seconds: Double
        public let speechSeconds: Double
        public let floorDb: Int?
        public let peakDb: Int?
        public let isDigitalSilence: Bool
    }

    public static func speechStats(_ pcm: Data) -> SpeechStats {
        var levels: [Double] = []
        var peak = 0
        var scratch = [Float](repeating: 0, count: windowSamples)
        pcm.withUnsafeBytes { raw in
            let samples = raw.bindMemory(to: Int16.self)
            var start = 0
            while start < samples.count {
                let end = min(samples.count, start + windowSamples)
                let w = level(UnsafeBufferPointer(rebasing: samples[start..<end]), scratch: &scratch)
                levels.append(w.db)
                peak = max(peak, w.peak)
                start = end
            }
        }
        let finite = levels.filter(\.isFinite).sorted()
        let floor = finite.isEmpty ? -Double.infinity : finite[Int(Double(finite.count) * 0.1)]
        let threshold = max(-50, (floor.isFinite ? floor : -90) + 10)
        let speechWindows = levels.filter { $0 > threshold }.count
        return SpeechStats(
            seconds: Double(pcm.count) / Double(bytesPerSecond),
            speechSeconds: Double(speechWindows) * Double(windowSamples) / Double(sampleRate),
            floorDb: floor.isFinite ? jsRound(floor) : nil,
            peakDb: peak > 0 ? jsRound(20 * log10(Double(peak) / 32768)) : nil,
            isDigitalSilence: peak == 0)
    }

    /// Math.round: halves go up, also below zero.
    static func jsRound(_ x: Double) -> Int { Int((x + 0.5).rounded(.down)) }

    // MARK: FLAC

    /// A recording longer than 30 s goes up as FLAC: lossless, half the bytes, and at 10 minutes
    /// the answer comes twice as fast (measured 2026-10-03: 15.8 s WAV, 7.7 s FLAC, same text).
    /// The file is closed before it is read: a FLAC whose STREAMINFO says zero samples makes
    /// MAI answer 500. Nil when the encoder fails; the caller sends WAV then.
    public static func encodeFLAC(_ pcm: Data) -> Data? {
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16, sampleRate: Double(sampleRate), channels: 1, interleaved: true),
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(pcm.count / 2)),
            let channel = buffer.int16ChannelData?[0]
        else { return nil }
        buffer.frameLength = AVAudioFrameCount(pcm.count / 2)
        pcm.withUnsafeBytes { raw in
            if let base = raw.baseAddress { memcpy(channel, base, Int(buffer.frameLength) * 2) }
        }
        let dir = FileManager.default.temporaryDirectory.appending(path: "claude-acc-flac-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let url = dir.appending(path: "upload.flac")
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatFLAC,
                AVSampleRateKey: Double(sampleRate),
                AVNumberOfChannelsKey: 1,
                AVEncoderBitDepthHintKey: 16,
            ]
            let file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatInt16, interleaved: true)
            try file.write(from: buffer)
            file.close()
            let flac = try Data(contentsOf: url)
            return flac.count > 0 ? flac : nil
        } catch {
            return nil
        }
    }
}
