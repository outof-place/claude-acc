import SwiftUI

@main
struct ClaudeAccApp: App {
    @State private var store = Store()

    init() {
        // `ClaudeAcc --render plik.png [--snapshot dane.json]`: panel do pliku, do oglądania
        // bez klikania; z --snapshot na danych z pliku zamiast żywych (zrzuty do README)
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render"), i + 1 < args.count {
            let data = args.firstIndex(of: "--snapshot").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            exit(Self.render(to: args[i + 1], from: data) ? 0 : 1)
        }
        // druga kopia aplikacji dałaby drugi pierścień w pasku menu
        let mine = Bundle.main.bundleIdentifier ?? ""
        if NSRunningApplication.runningApplications(withBundleIdentifier: mine).count > 1 {
            exit(0)
        }
    }

    var body: some Scene {
        MenuBarExtra {
            PanelView(store: store)
        } label: {
            MenuBarLabel(store: store)
        }
        .menuBarExtraStyle(.window)
    }

    @MainActor
    private static func render(to path: String, from snapshotFile: String?) -> Bool {
        var output = CLIResult(status: -1, stdout: "", stderr: "")
        if let snapshotFile {
            let text = (try? String(contentsOfFile: snapshotFile, encoding: .utf8)) ?? ""
            output = CLIResult(status: text.isEmpty ? 1 : 0, stdout: text, stderr: "nie ma pliku \(snapshotFile)")
        } else {
            let done = DispatchSemaphore(value: 0)
            Task.detached {
                output = await CLI.run(["status", "--json"])
                done.signal()
            }
            done.wait()
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard output.status == 0,
              let snapshot = try? decoder.decode(Snapshot.self, from: Data(output.stdout.utf8)) else {
            FileHandle.standardError.write(Data("brak danych: \(output.message)\n".utf8))
            return false
        }
        let panel = PanelView(store: Store(preview: snapshot))
            .background(Color(nsColor: .windowBackgroundColor))
            .environment(\.colorScheme, .dark)
        let renderer = ImageRenderer(content: panel)
        renderer.scale = 2
        guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
              let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) else {
            return false
        }
        return FileManager.default.createFile(atPath: path, contents: png)
    }
}
