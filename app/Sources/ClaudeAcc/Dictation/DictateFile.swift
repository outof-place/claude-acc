import AppKit
import DictationCore
import Foundation

/// `--dictate-file <wav>`: the pipeline on a file, printed as JSON, then exit. Runs before
/// the single-instance check, on a main-actor task inside a run loop of its own.
enum DictateFile {
    static func run(_ path: String) -> Never {
        Task { @MainActor in
            let dictation = Dictation(preview: true)
            var out: [String: Any] = ["file": path]
            var status: Int32 = 0
            do {
                let wav = try Data(contentsOf: URL(fileURLWithPath: path))
                guard let key = await Dictation.readKey() else { throw DictateError("no_key", "No AI_GATEWAY_API_KEY in the Keychain") }
                let started = ContinuousClock.now
                let r = try await Pipeline.run(
                    pcm: Audio.wavToPcm(wav), options: dictation.pipelineOptions, service: Gateway(key: key))
                out.merge([
                    "text": r.text, "raw": r.raw ?? NSNull(), "model": r.model, "attempt": r.attempt, "format": r.format,
                    "seconds": r.seconds, "hedged": r.hedged, "stt_ms": r.sttMs, "correction_ms": r.correctionMs ?? NSNull(),
                    "total_ms": started.duration(to: .now).milliseconds,
                ]) { $1 }
            } catch let error as DictateError {
                out.merge(["error": error.code, "message": error.message]) { $1 }
                status = 1
            } catch {
                out.merge(["error": "internal", "message": error.localizedDescription]) { $1 }
                status = 1
            }
            let data = (try? JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])) ?? Data()
            FileHandle.standardOutput.write(data + Data("\n".utf8))
            exit(status)
        }
        CFRunLoopRun()
        exit(1)
    }
}

/// `--widget-demo`: the dictation pill through its states, then exit.
enum WidgetDemo {
    static func run() -> Never {
        Task { @MainActor in
            NSApplication.shared.setActivationPolicy(.accessory)
            let dictation = Dictation(preview: true)
            let widget = DictationWidget(dictation: dictation)
            await dictation.runDemo()
            withExtendedLifetime(widget) {}
            exit(0)
        }
        NSApplication.shared.run()
        exit(1)
    }
}
