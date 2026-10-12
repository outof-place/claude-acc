import SwiftUI

@main
struct ClaudeAccApp: App {
    @NSApplicationDelegateAdaptor private var delegate: AppDelegate

    init() {
        // `ClaudeAcc --render panel.png [--snapshot accounts.json] [--live]`: the panel as a picture,
        // to look at without clicking. With --snapshot it uses that file instead of live data,
        // and demo-guard.json / demo-janitor.json / demo-updates.json next to it, if they exist (README screenshots).
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--render"), i + 1 < args.count {
            let data = args.firstIndex(of: "--snapshot").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            let open = args.firstIndex(of: "--open").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            let hover = args.firstIndex(of: "--hover").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            // --live keeps the panel's fixed height, so a column that overflows shows up as it does on
            // screen (lists that scroll inside their cards render empty: ImageRenderer skips ScrollView)
            let live = args.contains("--live")
            exit(Self.render(to: args[i + 1], from: data, open: open, hover: hover, live: live) ? 0 : 1)
        }
        // `ClaudeAcc --dictate-file test.wav`: one recording through dictation's whole path
        // (Keychain, AI Gateway, correction) without the microphone, as JSON; works while the app runs
        if let i = args.firstIndex(of: "--dictate-file"), i + 1 < args.count {
            DictateFile.run(args[i + 1])
        }
        if args.contains("--widget-demo") { WidgetDemo.run() }
        // a second copy would put a second ring in the menu bar
        PodMenu.keepOneCopy()
    }

    var body: some Scene {
        // the ring and the panel are AppKit (MenuBarController); an App still needs a scene
        Settings { EmptyView() }
    }

    private static func render(to path: String, from snapshotFile: String?, open: String?, hover: String?, live: Bool) -> Bool {
        let output: CLIResult
        var guardState: GuardState?
        var janitor: JanitorState?
        var fans: FanState?
        var ultra: Ultra?
        var load: LoadReading?
        var sched: SchedState?
        var depot: DepotRuns?
        var updates: UpdatesState?
        var desktop: DesktopPanel?
        var link: TetherLink?
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
            if let data = try? Data(contentsOf: folder.appending(path: "demo-depot.json")) {
                depot = Store.decode(DepotRuns.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-updates.json")) {
                updates = Store.decode(UpdatesState.self, from: data)
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-perf.json")) {
                let perf = Store.decode(PerfFile.self, from: data)
                ultra = perf?.ultra
                link = perf?.link
            }
            if let data = try? Data(contentsOf: folder.appending(path: "demo-desktop.json")) {
                desktop = Store.decode(DesktopPanel.self, from: data)
            }
        } else {
            output = CLI.runBlocking(CLI.process(["status", "--json"]))
            // Stay Awake's hotspot line: the route `perf keep` saw last (it runs every 5 minutes)
            link = FileManager.default.contents(atPath: CLI.perfState)
                .flatMap { Store.decode(PerfFile.self, from: $0) }?.link
        }
        guard output.status == 0, let snapshot = Store.decode(Snapshot.self, from: Data(output.stdout.utf8)) else {
            FileHandle.standardError.write(Data("no data: \(output.message)\n".utf8))
            return false
        }
        let store = Store(preview: snapshot, guardState: guardState, janitor: janitor, fans: fans, ultra: ultra, load: load, sched: sched, depot: depot, updates: updates, desktop: desktop, link: link)
        store.previewOpenAccount = open
        store.previewHoverAccount = hover
        let frozen = snapshotFile.map { _ in Date(timeIntervalSince1970: snapshot.generatedAt) }
        let panel = PanelView(store: store, frozenNow: frozen)
            .fixedSize()
            .environment(\.renderingToFile, !live)
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
    private var presence: MenuBarPresence?
    private var widget: DictationWidget?
    private var store: Store?
    /// URLs that arrive before the store exists: `open -g` that starts the app delivers its URL early.
    private var early: [URL] = []
    private var termination: TerminationSignal?

    func applicationWillFinishLaunching(_ notification: Notification) {
        termination = TerminationSignal { MainActor.assumeIsolated { NSApp.terminate(nil) } }
        // `claude-acc dictate` opens claude-acc://dictate/<toggle|start|stop|cancel> in the background
        NSAppleEventManager.shared().setEventHandler(
            self, andSelector: #selector(openURL(_:reply:)),
            forEventClass: AEEventClass(kInternetEventClass), andEventID: AEEventID(kAEGetURL))
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let store = Store()
        self.store = store
        let menuBar = MenuBarController(store: store)
        self.menuBar = menuBar
        // Pod's copy hands the ring to Pod's native shell when the user picks it; the standalone one
        // always shows it
        let presence = MenuBarPresence { [weak menuBar] shown in menuBar?.setShown(shown) }
        self.presence = presence
        presence.start()
        widget = DictationWidget(dictation: store.dictation)
        store.dictation.activate()
        early.forEach(handle)
        early = []
    }

    func applicationWillTerminate(_ notification: Notification) {
        presence?.stop()
    }

    @objc private func openURL(_ event: NSAppleEventDescriptor, reply: NSAppleEventDescriptor) {
        guard let text = event.paramDescriptor(forKeyword: keyDirectObject)?.stringValue,
              let url = URL(string: text), url.scheme == "claude-acc"
        else { return }
        if store == nil { early.append(url) } else { handle(url) }
    }

    private func handle(_ url: URL) {
        guard let store else { return }
        switch url.host() {
        // `claude-acc awake on|off|toggle`: Stay Awake from the terminal or Orca
        case "awake": store.awake.command(url)
        // `claude-acc awake lid on|off`: the lid-closed setting, from Pod's native window
        case "awake-lid": store.awake.lidCommand(url)
        // claude-acc://panel[/<section>]: Pod's "claude-acc settings…" and `claude-acc panel`
        case "panel": menuBar?.showPanel(section: PanelRoute.section(of: url))
        case "dictate":
            dictationLog.notice("command: \(url.absoluteString, privacy: .public)")
            switch url.lastPathComponent {
            case "start": store.dictation.start(handsFree: true, fromCommand: true)
            case "stop": store.dictation.stop()
            case "cancel": store.dictation.cancel()
            default: store.dictation.toggle()
            }
        default: break
        }
    }
}
