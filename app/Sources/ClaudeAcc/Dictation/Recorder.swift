import AVFoundation
import CoreAudio
import DictationCore
import Synchronization

/// What the microphone reports while it records.
enum RecorderEvent: Sendable {
    case firstBuffer
    /// One 100 ms window: where the recording is and how loud.
    case level(ms: Int, db: Double)
    /// 1.5 s of nothing but zeros: the microphone is there but gives no sound (no permission).
    case digitalSilence
    /// The length limit: the recording stops itself.
    case limit
}

/// Input devices through Core Audio, for the picker and to pick one by its UID.
struct InputDevice: Identifiable, Hashable, Sendable {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let isBuiltIn: Bool
    let isBluetooth: Bool

    /// Teams, Zoom, BlackHole and friends: never a fallback.
    var isVirtual: Bool {
        name.range(of: "teams|zoom|blackhole|loopback|virtual|speakeramp|obs|soundflower|aggregate|krisp",
                   options: [.regularExpression, .caseInsensitive]) != nil
    }

    static func all() -> [InputDevice] {
        var size: UInt32 = 0
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr
        else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr
        else { return [] }
        return ids.compactMap(device)
    }

    static func defaultInput() -> InputDevice? {
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice, mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &id) == noErr
        else { return nil }
        return device(id)
    }

    private static func device(_ id: AudioDeviceID) -> InputDevice? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreams, mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else { return nil }
        guard let uid = string(id, kAudioDevicePropertyDeviceUID), let name = string(id, kAudioObjectPropertyName)
        else { return nil }
        var transport: UInt32 = 0
        size = UInt32(MemoryLayout<UInt32>.size)
        address.mSelector = kAudioDevicePropertyTransportType
        address.mScope = kAudioObjectPropertyScopeGlobal
        AudioObjectGetPropertyData(id, &address, 0, nil, &size, &transport)
        return InputDevice(
            id: id, uid: uid, name: name, isBuiltIn: transport == kAudioDeviceTransportTypeBuiltIn,
            isBluetooth: transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE)
    }

    private static func string(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String? {
        var address = AudioObjectPropertyAddress(
            mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr, let value else { return nil }
        return value.takeRetainedValue() as String
    }
}

/// Where the tap's buffers go. The tap runs on an audio thread, so this is the one object it
/// touches: the state sits behind a mutex, the levels leave through a stream the main actor
/// reads.
nonisolated final class CaptureSink: Sendable {
    struct State {
        var pcm = Data()
        /// Samples of a window not yet complete.
        var window: [Int16] = []
        var samples = 0
        var peak = 0
        var started = false
        var warnedSilence = false
        var hitLimit = false
        var scratch = [Float](repeating: 0, count: Audio.windowSamples)
    }

    let state = Mutex(State())
    let events: AsyncStream<RecorderEvent>.Continuation
    let maxSamples: Int
    private let output = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: Double(Audio.sampleRate), channels: 1, interleaved: true)!
    /// Swapped by the main actor only while the tap is off (a device change); read by the tap.
    private let converterBox = Mutex<UnsafeConverter?>(nil)

    init(events: AsyncStream<RecorderEvent>.Continuation, maxSeconds: Int) {
        self.events = events
        maxSamples = maxSeconds * Audio.sampleRate
    }

    func use(input: AVAudioFormat) -> Bool {
        guard let converter = AVAudioConverter(from: input, to: output) else { return false }
        converterBox.withLock { $0 = UnsafeConverter(converter: converter) }
        return true
    }

    /// The tap block, built outside the main actor: one written in a main-actor type would
    /// check its executor on the audio thread and crash.
    static func makeTap(_ sink: CaptureSink) -> AVAudioNodeTapBlock {
        { buffer, _ in sink.consume(buffer) }
    }

    func consume(_ buffer: AVAudioPCMBuffer) {
        guard let converter = converterBox.withLock({ $0 })?.converter else { return }
        let ratio = output.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: capacity) else { return }
        var fed = false
        var error: NSError?
        // the buffer once, then "no data now": the converter keeps its resampler state for the next one
        converter.convert(to: out, error: &error) { _, status in
            if fed {
                status.pointee = .noDataNow
                return nil
            }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0, let channel = out.int16ChannelData?[0] else { return }
        let fresh = UnsafeBufferPointer(start: channel, count: Int(out.frameLength))

        var pending: [RecorderEvent] = []
        state.withLock { s in
            guard !s.hitLimit else { return }
            if !s.started {
                s.started = true
                pending.append(.firstBuffer)
            }
            let room = max(0, maxSamples - s.samples)
            let taken = UnsafeBufferPointer(rebasing: fresh.prefix(room))
            s.pcm.append(UnsafeBufferPointer(start: taken.baseAddress, count: taken.count))
            s.samples += taken.count
            s.window.append(contentsOf: taken)
            while s.window.count >= Audio.windowSamples {
                let level = s.window.withUnsafeBufferPointer {
                    Audio.level(UnsafeBufferPointer(rebasing: $0.prefix(Audio.windowSamples)), scratch: &s.scratch)
                }
                s.window.removeFirst(Audio.windowSamples)
                s.peak = max(s.peak, level.peak)
                let ms = (s.samples - s.window.count) * 1000 / Audio.sampleRate
                pending.append(.level(ms: ms, db: level.db.isFinite ? level.db.rounded() : -120))
            }
            if !s.warnedSilence, s.samples >= Audio.sampleRate * 3 / 2, s.peak == 0 {
                s.warnedSilence = true
                pending.append(.digitalSilence)
            }
            if s.samples >= maxSamples {
                s.hitLimit = true
                pending.append(.limit)
            }
        }
        for event in pending { events.yield(event) }
    }

    /// The recording so far, handed over without a copy.
    func take() -> (pcm: Data, hitLimit: Bool) {
        state.withLock { s in
            var pcm = Data()
            swap(&pcm, &s.pcm)
            return (pcm, s.hitLimit)
        }
    }
}

/// AVAudioConverter isn't Sendable; this one is used by the tap thread alone.
private struct UnsafeConverter: @unchecked Sendable {
    let converter: AVAudioConverter
}

/// The microphone through AVAudioEngine, as 16 kHz mono 16-bit PCM in memory (ten minutes is 19 MB).
final class Recorder {
    private var engine: AVAudioEngine?
    private var sink: CaptureSink?
    private var configObserver: (any NSObjectProtocol)?
    private(set) var deviceName: String?
    /// The engine's own failure while recording (a device that went away).
    var onFailure: ((DictateError) -> Void)?

    var isRecording: Bool { sink != nil }

    /// Starts recording. `deviceUID` nil is the system input; `preferBuiltIn` keeps a
    /// Bluetooth headset in its music profile by recording from the Mac instead.
    func start(deviceUID: String?, preferBuiltIn: Bool, maxSeconds: Int) throws -> AsyncStream<RecorderEvent> {
        cancel()
        let (stream, continuation) = AsyncStream<RecorderEvent>.makeStream(bufferingPolicy: .bufferingNewest(64))
        let sink = CaptureSink(events: continuation, maxSeconds: maxSeconds)
        let engine = self.engine ?? AVAudioEngine()
        self.engine = engine
        let device = pickDevice(uid: deviceUID, preferBuiltIn: preferBuiltIn)
        deviceName = device?.name
        if let device { try use(device, on: engine) }
        try install(sink, on: engine)
        engine.prepare()
        do {
            try engine.start()
        } catch {
            engine.inputNode.removeTap(onBus: 0)
            throw DictateError("device", "The microphone didn't start: \(error.localizedDescription)")
        }
        self.sink = sink
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.configurationChanged() }
        }
        return stream
    }

    /// Stops and hands over the recording; the microphone light goes out here.
    func finish() -> (pcm: Data, hitLimit: Bool) {
        guard let sink else { return (Data(), false) }
        teardown()
        let taken = sink.take()
        sink.events.finish()
        return taken
    }

    func cancel() {
        guard let sink else { return }
        teardown()
        _ = sink.take()
        sink.events.finish()
    }

    private func teardown() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        sink = nil
    }

    private func install(_ sink: CaptureSink, on engine: AVAudioEngine) throws {
        let input = engine.inputNode.outputFormat(forBus: 0)
        guard input.sampleRate > 0, input.channelCount > 0 else {
            throw DictateError("device", "No input: the microphone reports no audio format")
        }
        guard sink.use(input: input) else { throw DictateError("device", "No converter from \(input)") }
        engine.inputNode.removeTap(onBus: 0)
        engine.inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil, block: CaptureSink.makeTap(sink))
    }

    private func use(_ device: InputDevice, on engine: AVAudioEngine) throws {
        guard let unit = engine.inputNode.audioUnit else { return }
        var id = device.id
        let status = AudioUnitSetProperty(
            unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &id,
            UInt32(MemoryLayout<AudioDeviceID>.size))
        if status != noErr { throw DictateError("device", "Can't record from \(device.name) (\(status))") }
    }

    private func pickDevice(uid: String?, preferBuiltIn: Bool) -> InputDevice? {
        let devices = InputDevice.all()
        if let uid, let picked = devices.first(where: { $0.uid == uid }) { return picked }
        let system = InputDevice.defaultInput()
        if preferBuiltIn, system?.isBluetooth == true, let builtIn = devices.first(where: \.isBuiltIn) {
            return builtIn
        }
        // a stale pick of a device that's gone: the system input, unless that's a virtual one
        if uid != nil, let system, system.isVirtual {
            return devices.first { !$0.isVirtual && !$0.isBluetooth } ?? system
        }
        return nil
    }

    /// AirPods connecting or a sample rate change stop the engine without a word: the
    /// converter is rebuilt for the new format and recording goes on into the same buffer.
    private func configurationChanged() {
        guard let engine, let sink else {
            // idle: the next recording builds a fresh engine for the new hardware
            engine = nil
            return
        }
        do {
            engine.inputNode.removeTap(onBus: 0)
            try install(sink, on: engine)
            engine.prepare()
            try engine.start()
        } catch {
            let failure = (error as? DictateError) ?? DictateError("audio", error.localizedDescription)
            cancel()
            onFailure?(failure)
        }
    }
}
