import AppKit
import DictationCore
import SwiftUI

/// The dictation pill. It isn't there until you dictate: holding the right ⌘ brings it in with
/// a spring, it grows into a recorder, morphs through transcribing and done, and leaves again.
/// It floats over every window and Space, full-screen apps too, and never takes the keyboard,
/// so the cursor stays where the text should go.
///
/// The window has one fixed size and the pill sits in its middle: the pill changes width with
/// a SwiftUI spring instead of the window jumping, and WindowServer lets clicks on the
/// transparent rest through to the app below (only drawn pixels catch them). Coming and going
/// are Core Animation on the pill's layer, so they run in the render server, smooth whatever
/// the main thread is doing. Hidden, the window is ordered out: nothing draws, nothing ticks.
final class DictationWidget {
    private static let windowSize = CGSize(width: 420, height: 64)
    private static let showDuration = 0.42
    private static let hideDuration = 0.2

    private let dictation: Dictation
    private let panel = WidgetPanel()
    private let container = WidgetContainer()
    private let host: NSView
    /// The window's centre on screen, saved between launches.
    private var anchor: CGPoint
    private var isShown = false

    init(dictation: Dictation) {
        self.dictation = dictation
        let saved = UserDefaults.standard.string(forKey: "dictation.widget.anchor").map(NSPointFromString)
        anchor = saved ?? Self.defaultAnchor()
        let hosting = WidgetHostingView(rootView: WidgetView(dictation: dictation))
        // the window's size is ours: the hosting view must not drive it
        hosting.sizingOptions = []
        host = hosting
        container.frame = CGRect(origin: .zero, size: Self.windowSize)
        container.wantsLayer = true
        hosting.frame = container.bounds
        hosting.autoresizingMask = [.width, .height]
        hosting.wantsLayer = true
        hosting.layer?.opacity = 0
        container.addSubview(hosting)
        panel.contentView = container
        container.onClick = { [weak dictation] in dictation?.toggle() }
        container.onMenu = { [weak self] event in self?.showMenu(event) }
        container.onMoved = { [weak self] in self?.moved() }
        container.isInteractive = { [weak dictation] in dictation?.phase != .error }
        place()
        observe()
    }

    /// Shown while dictating, or always when the person asked for it, or while Accessibility is
    /// missing (the dot says so).
    private var wantsVisible: Bool {
        dictation.phase != .idle || dictation.widgetWhenIdle || !dictation.trusted
    }

    /// Follows `wantsVisible` through Observation: one callback per change, nothing in between.
    private func observe() {
        let visible = withObservationTracking { wantsVisible } onChange: { [weak self] in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.observe() } }
        }
        visible ? show() : hide()
    }

    private static func defaultAnchor() -> CGPoint {
        let area = NSScreen.main?.visibleFrame ?? .init(x: 0, y: 0, width: 1440, height: 900)
        // above the Dock, also when an auto-hidden one slides in
        return CGPoint(x: area.midX, y: area.minY + 96)
    }

    /// Centred on the anchor, inside the visible area of the anchor's screen.
    private func place() {
        let screen = NSScreen.screens.first { $0.frame.contains(anchor) } ?? NSScreen.main
        let area = screen?.visibleFrame ?? .init(x: 0, y: 0, width: 1440, height: 900)
        let size = Self.windowSize
        var frame = CGRect(x: anchor.x - size.width / 2, y: anchor.y - size.height / 2, width: size.width, height: size.height)
        frame.origin.x = min(max(frame.minX, area.minX), area.maxX - frame.width)
        frame.origin.y = min(max(frame.minY, area.minY), area.maxY - frame.height)
        panel.setFrame(frame.integral, display: false)
    }

    // MARK: Coming and going

    /// Springs up from a little below, scaled down, fading in; picks up from wherever a
    /// hide left off.
    private func show() {
        guard let layer = host.layer, !isShown else { return }
        isShown = true
        let from = layer.presentation() ?? layer
        let midway = layer.animation(forKey: "out") != nil
        let startOpacity = midway ? from.opacity : 0
        let startTransform = midway ? from.transform : Self.transform(scale: 0.82, lift: -10)
        layer.removeAllAnimations()
        layer.opacity = 1
        layer.transform = CATransform3DIdentity
        if !panel.isVisible { panel.orderFrontRegardless() }

        let spring = CASpringAnimation(perceptualDuration: Self.showDuration, bounce: 0.22)
        spring.keyPath = "transform"
        spring.fromValue = NSValue(caTransform3D: startTransform)
        spring.toValue = NSValue(caTransform3D: CATransform3DIdentity)
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = startOpacity
        fade.toValue = 1
        fade.duration = 0.16
        fade.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1, 0.36, 1)
        layer.add(spring, forKey: "in")
        layer.add(fade, forKey: "fade-in")
    }

    /// Sinks a little and fades, then the window leaves the screen.
    private func hide() {
        guard let layer = host.layer, isShown else {
            if !isShown, panel.isVisible, host.layer?.animation(forKey: "out") == nil { panel.orderOut(nil) }
            return
        }
        isShown = false
        let from = layer.presentation() ?? layer
        let end = Self.transform(scale: 0.9, lift: -6)
        layer.removeAllAnimations()
        layer.opacity = 0
        layer.transform = end

        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            MainActor.assumeIsolated {
                // shown again during the fade: it stays
                guard let self, !self.isShown else { return }
                self.panel.orderOut(nil)
            }
        }
        let curve = CAMediaTimingFunction(controlPoints: 0.4, 0, 1, 1)
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = from.opacity
        fade.toValue = 0
        fade.duration = Self.hideDuration
        fade.timingFunction = curve
        let sink = CABasicAnimation(keyPath: "transform")
        sink.fromValue = NSValue(caTransform3D: from.transform)
        sink.toValue = NSValue(caTransform3D: end)
        sink.duration = Self.hideDuration
        sink.timingFunction = curve
        layer.add(fade, forKey: "out")
        layer.add(sink, forKey: "sink")
        CATransaction.commit()
    }

    /// Scale about the window's centre (AppKit layers scale about their bottom-left origin),
    /// then shift up by `lift` (negative: down).
    private static func transform(scale: CGFloat, lift: CGFloat) -> CATransform3D {
        let size = windowSize
        let scaled = CATransform3DMakeScale(scale, scale, 1)
        let shift = CATransform3DMakeTranslation(size.width * (1 - scale) / 2, size.height * (1 - scale) / 2 + lift, 0)
        return CATransform3DConcat(scaled, shift)
    }

    // MARK: Moving and the menu

    private func moved() {
        anchor = CGPoint(x: panel.frame.midX, y: panel.frame.midY)
        UserDefaults.standard.set(NSStringFromPoint(anchor), forKey: "dictation.widget.anchor")
    }

    private func showMenu(_ event: NSEvent) {
        let menu = NSMenu()
        let insert = menu.addItem(withTitle: "Insert Last Transcript", action: #selector(MenuTarget.run(_:)), keyEquivalent: "")
        insert.isEnabled = dictation.lastText != nil
        insert.representedObject = MenuAction { [weak dictation] in dictation?.insertLast() }
        if dictation.isActive || dictation.phase == .transcribing {
            menu.addItem(withTitle: "Cancel", action: #selector(MenuTarget.run(_:)), keyEquivalent: "")
                .representedObject = MenuAction { [weak dictation] in dictation?.cancel() }
        }
        menu.addItem(.separator())
        let idle = menu.addItem(withTitle: "Show While Idle", action: #selector(MenuTarget.run(_:)), keyEquivalent: "")
        idle.state = dictation.widgetWhenIdle ? .on : .off
        idle.representedObject = MenuAction { [weak dictation] in dictation?.widgetWhenIdle.toggle() }
        menu.addItem(withTitle: "Reset Position", action: #selector(MenuTarget.run(_:)), keyEquivalent: "")
            .representedObject = MenuAction { [weak self] in
                guard let self else { return }
                self.anchor = Self.defaultAnchor()
                UserDefaults.standard.removeObject(forKey: "dictation.widget.anchor")
                self.place()
            }
        for item in menu.items { item.target = MenuTarget.shared }
        NSMenu.popUpContextMenu(menu, with: event, for: container)
    }
}

private final class MenuAction {
    let run: () -> Void
    init(_ run: @escaping () -> Void) { self.run = run }
}

private final class MenuTarget: NSObject {
    static let shared = MenuTarget()
    @objc func run(_ sender: NSMenuItem) { (sender.representedObject as? MenuAction)?.run() }
}

/// Borderless, non-activating, never key: over everything (`.statusBar` keeps it under system
/// alerts) on every Space and beside full-screen apps.
private final class WidgetPanel: NSPanel {
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .statusBar
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        hidesOnDeactivate = false
        isMovable = false
        animationBehavior = .none
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Clicks and drags on the pill. A click toggles dictation, a drag moves the window, a right
/// click opens the menu. In the error state the SwiftUI buttons get the clicks.
private final class WidgetContainer: NSView {
    var onClick: (() -> Void)?
    var onMenu: ((NSEvent) -> Void)?
    var onMoved: (() -> Void)?
    var isInteractive: (() -> Bool)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard bounds.contains(convert(point, from: superview)) else { return nil }
        // the error state has buttons: SwiftUI takes the click
        if isInteractive?() == false { return super.hitTest(point) }
        return self
    }

    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        let start = NSEvent.mouseLocation
        let origin = window.frame.origin
        var dragged = false
        while let next = window.nextEvent(matching: [.leftMouseDragged, .leftMouseUp]) {
            let now = NSEvent.mouseLocation
            if next.type == .leftMouseUp { break }
            if !dragged, hypot(now.x - start.x, now.y - start.y) < 3 { continue }
            dragged = true
            window.setFrameOrigin(NSPoint(x: origin.x + now.x - start.x, y: origin.y + now.y - start.y))
        }
        if dragged { onMoved?() } else { onClick?() }
    }

    override func rightMouseDown(with event: NSEvent) {
        onMenu?(event)
    }
}

private final class WidgetHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

// MARK: - The pill

/// One capsule for every state: its width follows the content with a spring, and what's
/// inside swaps with a blur, so going from recording to transcribing to done reads as one
/// object changing, not windows popping.
private struct WidgetView: View {
    let dictation: Dictation

    var body: some View {
        HStack(spacing: 7) {
            content
                .transition(.blurReplace.combined(with: .scale(0.9)))
        }
        .font(.system(size: 12, weight: .medium, design: .rounded))
        .foregroundStyle(.white)
        .padding(.horizontal, dictation.phase == .idle ? 0 : 12)
        .frame(minWidth: 30, minHeight: 30)
        .fixedSize()
        .background(Color(white: 0.09).opacity(0.92), in: .capsule)
        .overlay { Capsule().strokeBorder(ring.opacity(0.7), lineWidth: ring == .white ? 0.5 : 1) }
        .shadow(color: .black.opacity(0.45), radius: 3, y: 1)
        .geometryGroup()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .environment(\.colorScheme, .dark)
        .animation(.spring(duration: 0.38, bounce: 0.18), value: dictation.phase)
        .animation(.spring(duration: 0.3, bounce: 0.1), value: dictation.note)
    }

    private var ring: Color {
        switch dictation.phase {
        case .idle: dictation.trusted ? .white : .orange
        case .starting, .recording, .stopping: .red
        case .transcribing: .white
        case .done: dictation.onClipboard ? .orange : .green
        case .error: dictation.failure?.isSoft == true ? .yellow : .red
        }
    }

    @ViewBuilder private var content: some View {
        switch dictation.phase {
        case .idle:
            Image(systemName: dictation.trusted ? "mic.fill" : "exclamationmark")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(dictation.trusted ? Color.white.opacity(0.85) : .orange)
                .frame(width: 30, height: 30)
                .help(dictation.trusted ? "Tap right ⌘ to dictate, tap again to insert" : "Dictation needs Accessibility: click to allow")
        case .starting, .recording, .stopping:
            Recording(dictation: dictation)
        case .transcribing:
            Transcribing(dictation: dictation)
        case .done:
            Image(systemName: dictation.onClipboard ? "doc.on.clipboard" : "checkmark")
                .foregroundStyle(dictation.onClipboard ? .orange : .green)
                .fontWeight(.bold)
            Text(dictation.onClipboard
                 ? "On the clipboard · ⌘V"
                 : "\(dictation.lastWords) \(dictation.lastWords == 1 ? "word" : "words") · \(TextRules.formatSeconds(ms: dictation.lastMs))")
                .monospacedDigit()
        case .error:
            Failure(dictation: dictation)
        }
    }
}

private struct Recording: View {
    let dictation: Dictation

    var body: some View {
        PulsingDot(isPulsing: dictation.phase == .recording)
            .frame(width: 8, height: 8)
        if dictation.phase == .starting {
            Text("Listening…").foregroundStyle(.white.opacity(0.7))
        } else {
            Text(TextRules.formatClock(ms: dictation.elapsedSeconds * 1000))
                .monospacedDigit()
            Wave(levels: dictation.levels)
                .frame(width: 72, height: 16)
            if dictation.handsFree {
                Image(systemName: "lock.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(.white.opacity(0.6))
                    .help("Hands free: tap right ⌘ to stop")
            }
        }
        if let note = dictation.note {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.yellow)
                .help(note)
                .transition(.scale.combined(with: .opacity))
        }
    }
}

/// The red recording dot, breathing. Core Animation runs the breath in the render server: a
/// continuous symbol effect made SwiftUI redraw its host on every frame of the recording.
private struct PulsingDot: NSViewRepresentable {
    let isPulsing: Bool

    func makeNSView(context: Context) -> PulsingDotView { PulsingDotView() }
    func updateNSView(_ view: PulsingDotView, context: Context) { view.isPulsing = isPulsing }
}

private final class PulsingDotView: NSView {
    var isPulsing = false {
        didSet {
            guard isPulsing != oldValue, let layer else { return }
            if isPulsing {
                let breath = CABasicAnimation(keyPath: "opacity")
                breath.fromValue = 1
                breath.toValue = 0.3
                breath.duration = 0.7
                breath.autoreverses = true
                breath.repeatCount = .infinity
                breath.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                layer.add(breath, forKey: "breath")
            } else {
                layer.removeAnimation(forKey: "breath")
            }
        }
    }

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = NSColor.systemRed.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        layer?.cornerRadius = min(bounds.width, bounds.height) / 2
    }
}

/// The last levels as bars: -60 dBFS is the floor, -12 dBFS the top. One Canvas, one draw per
/// 100 ms window, no views per bar.
private struct Wave: View {
    let levels: [Double]
    private static let bars = 18

    var body: some View {
        Canvas { context, size in
            let tail = levels.suffix(Self.bars)
            let pad = Self.bars - tail.count
            let step = size.width / CGFloat(Self.bars)
            var bars = Path()
            for (i, db) in ([Double](repeating: -90, count: pad) + tail).enumerated() {
                let x = min(1, max(0, (db + 60) / 48))
                let height = max(2, size.height * x)
                let rect = CGRect(
                    x: CGFloat(i) * step + step * 0.2, y: (size.height - height) / 2,
                    width: step * 0.6, height: height)
                bars.addRoundedRect(in: rect, cornerSize: CGSize(width: step * 0.3, height: step * 0.3))
            }
            context.fill(bars, with: .color(.red.opacity(0.9)))
        }
    }
}

private struct Transcribing: View {
    let dictation: Dictation

    var body: some View {
        ProgressView().controlSize(.mini).tint(.white)
        TimelineView(.periodic(from: .now, by: 0.1)) { context in
            let ms = Int((dictation.transcribingSince.map { context.date.timeIntervalSince($0) } ?? 0) * 1000)
            Text(TextRules.formatSeconds(ms: ms)).monospacedDigit()
        }
        Text(dictation.correcting ? "correcting" : Models.label(dictation.activeModel)
             + (dictation.attempt > 1 ? " · try \(dictation.attempt)" : ""))
            .foregroundStyle(.white.opacity(0.6))
            .lineLimit(1)
            .contentTransition(.opacity)
    }
}

private struct Failure: View {
    let dictation: Dictation

    var body: some View {
        let failure = dictation.failure
        Image(systemName: "xmark.circle.fill")
            .foregroundStyle(failure?.isSoft == true ? .yellow : .red)
        Text(failure?.title ?? "Dictation failed")
            .lineLimit(1)
            .help(failure?.hint ?? "")
        if failure?.retryFile != nil {
            Button("Retry") { dictation.retry() }
                .buttonStyle(.plain)
                .foregroundStyle(Format.violet)
                .fontWeight(.semibold)
        }
        Button {
            dictation.dismiss()
        } label: {
            Image(systemName: "xmark").font(.system(size: 9, weight: .bold))
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white.opacity(0.6))
    }
}
