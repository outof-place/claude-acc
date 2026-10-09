import DictationCore
import SwiftUI

/// Dictation in the panel: state, permissions, models and the microphone.
struct DictationCard: View {
    let dictation: Dictation

    var body: some View {
        Card("Dictation", symbol: "waveform") {
            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(dictation.isActive ? Color.red : dictation.trusted ? Format.violet : .orange)
                        .contentTransition(.interpolate)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                permissions
                Button {
                    let starting = !dictation.isActive && dictation.phase != .transcribing
                    dictation.toggle()
                    if starting { NotificationCenter.default.post(name: Dictation.startedFromPanel, object: nil) }
                } label: {
                    Label(dictation.isActive ? "Stop and Insert" : "Dictate",
                          systemImage: dictation.isActive ? "stop.fill" : "mic.fill")
                        .frame(maxWidth: .infinity)
                }
                .panelButton(prominent: true)
                .disabled(!dictation.trusted || dictation.phase == .transcribing)
                .help("Records hands free into the app you were in; tap right ⌘ or click the pill to stop")
                VStack(spacing: 9) {
                    picker("Model", symbol: "text.bubble", selection: Binding(
                        get: { dictation.speechModel }, set: { dictation.speechModel = $0 }),
                        options: Models.speech.map { ($0, Models.label($0)) })
                    picker("Correction", symbol: "wand.and.sparkles", selection: Binding(
                        get: { dictation.correctionModel }, set: { dictation.correctionModel = $0 }),
                        options: [("auto", "Auto (Gemini 3.8 Flash)"), ("off", "Off")]
                            + Models.correction.map { ($0.id, Models.label($0.id) + ($0.zdr ? "" : " (no ZDR)")) })
                    picker("Microphone", symbol: "mic", selection: Binding(
                        get: { dictation.microphone }, set: { dictation.microphone = $0 }),
                        options: [("", "System input")] + dictation.devices.map { ($0.uid, $0.name) })
                    SettingRow("Zero data retention", symbol: "lock.shield", isOn: binding(\.zdr))
                    SettingRow("Built-in mic over Bluetooth", symbol: "airpods", isOn: binding(\.preferBuiltIn))
                    SettingRow("Sounds", symbol: "speaker.wave.2", isOn: binding(\.sounds))
                    SettingRow("Widget while idle", symbol: "capsule", isOn: binding(\.widgetWhenIdle))
                }
                HStack(spacing: 6) {
                    Button("Test", systemImage: "play.circle") { dictation.test() }
                        .panelButton()
                        .disabled(dictation.testing)
                    Button("Dictionary", systemImage: "character.book.closed") { openDictionary() }
                        .panelButton()
                    if dictation.lastText != nil {
                        Button("Insert Last", systemImage: "text.insert") { dictation.insertLast() }
                            .panelButton()
                    }
                }
                .controlSize(.small)
                if dictation.testing || dictation.testResult != nil {
                    Text(dictation.testing ? "Sending the test recording…" : dictation.testResult ?? "")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .textSelection(.enabled)
                }
            }
        } accessory: {
            Chip("right ⌘")
        }
        .onAppear { dictation.refreshDevices() }
        .animation(.smooth(duration: 0.3), value: dictation.phase)
    }

    private var title: String {
        switch dictation.phase {
        case .idle: dictation.trusted ? "Ready" : "Needs Accessibility"
        case .starting: "Opening the mic"
        case .recording, .stopping: "Recording \(TextRules.formatClock(ms: dictation.elapsedSeconds * 1000))"
        case .transcribing: "Transcribing"
        case .done: dictation.onClipboard ? "On the clipboard" : "Inserted"
        case .error: dictation.failure?.title ?? "Failed"
        }
    }

    private var detail: String {
        if dictation.phase == .error, let hint = dictation.failure?.hint { return hint }
        if let text = dictation.lastText {
            return "Last: \(dictation.lastWords) words in \(TextRules.formatSeconds(ms: dictation.lastMs)) via \(Models.label(dictation.lastModel)) · “\(text.prefix(60))\(text.count > 60 ? "…" : "")”"
        }
        return "Tap right ⌘ to start, tap it again to insert. Or hold it while you talk. Esc cancels."
    }

    @ViewBuilder private var permissions: some View {
        if !dictation.trusted || dictation.micStatus != .authorized {
            VStack(alignment: .leading, spacing: 6) {
                if !dictation.trusted {
                    Button(dictation.lostTrust ? "Accessibility Lost: Re-add Claude Acc" : "Allow Accessibility",
                           systemImage: "hand.raised") {
                        dictation.lostTrust ? dictation.openPrivacy("Privacy_Accessibility") : dictation.requestTrust()
                    }
                    .panelButton(prominent: true)
                    Text(dictation.lostTrust
                         ? "macOS keeps the old entry after an update: remove Claude Acc from the list with −, then add it again."
                         : "For the right ⌘ and to put the text at the cursor.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if dictation.micStatus != .authorized {
                    Button("Allow Microphone", systemImage: "mic.badge.plus") { dictation.requestMicrophone() }
                        .panelButton(prominent: dictation.trusted)
                }
            }
            .controlSize(.small)
        }
    }

    private func picker(
        _ title: String, symbol: String, selection: Binding<String>, options: [(String, String)]
    ) -> some View {
        HStack {
            Label(title, systemImage: symbol)
                .font(.callout)
                .labelStyle(PickerLabelStyle())
            Spacer(minLength: 8)
            Menu {
                ForEach(options, id: \.0) { option in
                    Button {
                        selection.wrappedValue = option.0
                    } label: {
                        if option.0 == selection.wrappedValue {
                            Label(option.1, systemImage: "checkmark")
                        } else {
                            Text(option.1)
                        }
                    }
                }
            } label: {
                Text(options.first { $0.0 == selection.wrappedValue }?.1 ?? selection.wrappedValue)
                    .font(.caption)
                    .lineLimit(1)
            }
            .menuStyle(.button)
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .fixedSize()
        }
    }

    private func binding(_ key: ReferenceWritableKeyPath<Dictation, Bool>) -> Binding<Bool> {
        Binding(get: { dictation[keyPath: key] }, set: { dictation[keyPath: key] = $0 })
    }

    /// The person's own terms: a file that survives updates, created on first open.
    private func openDictionary() {
        let file = Dictation.userTerms
        if !FileManager.default.fileExists(atPath: file.path) {
            try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? "# Własne terminy dyktowania, jeden w wierszu. Słownik z repo: slownik.txt obok.\n"
                .write(to: file, atomically: true, encoding: .utf8)
        }
        NSWorkspace.shared.open(file)
    }
}

private struct PickerLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 8) {
            configuration.icon
                .foregroundStyle(.secondary)
                .frame(width: 18)
            configuration.title
        }
    }
}
