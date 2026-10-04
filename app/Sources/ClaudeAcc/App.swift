import SwiftUI

@main
struct ClaudeAccApp: App {
    @State private var store = Store()

    init() {
        // `ClaudeAcc --render panel.png [--snapshot accounts.json]`: the panel as a picture,
        // to look at without clicking. With --snapshot it uses that file instead of live data,
        // and demo-guard.json / demo-janitor.json next to it, if they exist (README screenshots).
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render"), i + 1 < args.count {
            let data = args.firstIndex(of: "--snapshot").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            let open = args.firstIndex(of: "--open").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            exit(Self.render(to: args[i + 1], from: data, open: open) ? 0 : 1)
        }
        // a second copy would put a second ring in the menu bar
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

    private static func render(to path: String, from snapshotFile: String?, open: String?) -> Bool {
        let output: CLIResult
        var guardState: GuardState?
        var janitor: JanitorState?
        var fans: FanState?
        var ultra: Ultra?
        if let snapshotFile {
            let text = (try? String(contentsOfFile: snapshotFile, encoding: .utf8)) ?? ""
            output = CLIResult(status: text.isEmpty ? 1 : 0, stdout: text, stderr: "no file \(snapshotFile)")
            let folder = URL(fileURLWithPath: snapshotFile).deletingLastPathComponent()
            if let data = try? Data(contentsOf: folder.appending(path: "demo-guard.json")) {
                guardState = Store.decode(GuardState.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-janitor.json")) {
                janitor = Store.decode(JanitorState.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-fans.json")) {
                fans = Store.decode(FanState.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-perf.json")) {
                ultra = Store.decode(PerfFile.self, from: data)?.ultra
            }
        } else {
            output = CLI.runBlocking(CLI.process(["status", "--json"]))
        }
        guard output.status == 0, let snapshot = Store.decode(Snapshot.self, from: Data(output.stdout.utf8)) else {
            FileHandle.standardError.write(Data("no data: \(output.message)\n".utf8))
            return false
        }
        let store = Store(preview: snapshot, guardState: guardState, janitor: janitor, fans: fans, ultra: ultra)
        store.previewOpenAccount = open
        let frozen = snapshotFile.map { _ in Date(timeIntervalSince1970: snapshot.generatedAt) }
        let panel = PanelView(store: store, frozenNow: frozen)
            .fixedSize()
            .environment(\.renderingToFile, true)
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
