import SwiftUI

@main
struct ClaudeAccApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    init() {
        // `ClaudeAcc --render panel.png [--snapshot accounts.json]`: the panel as a picture,
        // to look at without clicking. With --snapshot it uses that file instead of live data,
        // and demo-guard.json / demo-janitor.json / demo-updates.json next to it, if they exist (README screenshots).
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render"), i + 1 < args.count {
            let data = args.firstIndex(of: "--snapshot").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            let open = args.firstIndex(of: "--open").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            let hover = args.firstIndex(of: "--hover").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            exit(Self.render(to: args[i + 1], from: data, open: open, hover: hover) ? 0 : 1)
        }
        // a second copy would put a second ring in the menu bar
        let mine = Bundle.main.bundleIdentifier ?? ""
        if NSRunningApplication.runningApplications(withBundleIdentifier: mine).count > 1 {
            exit(0)
        }
    }

    var body: some Scene {
        // the ring and the panel are AppKit (MenuBarController); an App still needs a scene
        Settings { EmptyView() }
    }

    private static func render(to path: String, from snapshotFile: String?, open: String?, hover: String?) -> Bool {
        let output: CLIResult
        var guardState: GuardState?
        var janitor: JanitorState?
        var fans: FanState?
        var ultra: Ultra?
        var load: LoadReading?
        var sched: SchedState?
        var updates: UpdatesState?
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
            if let data = try? Data(contentsOf: folder.appending(path: "demo-load.json")),
               let demo = try? JSONSerialization.jsonObject(with: data) as? [String: Double] {
                load = LoadReading(cpu: demo["cpu"] ?? 0, pCores: demo["p_cores"], eCores: demo["e_cores"], gpu: demo["gpu"])
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-sched.json")) {
                sched = Store.decode(SchedState.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-updates.json")) {
                updates = Store.decode(UpdatesState.self, from: data)
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
        let store = Store(preview: snapshot, guardState: guardState, janitor: janitor, fans: fans, ultra: ultra, load: load, sched: sched, updates: updates)
        store.previewOpenAccount = open
        store.previewHoverAccount = hover
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

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var menuBar: MenuBarController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        menuBar = MenuBarController(store: Store())
    }
}
