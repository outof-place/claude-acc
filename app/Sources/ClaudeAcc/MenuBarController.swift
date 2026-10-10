import AppKit
import SwiftUI

/// The ring in the menu bar and the panel under it. MenuBarExtra can neither place its window
/// nor animate it, and a panel wider than the room left of the ring ran off the screen. This
/// one is centred under the menu bar of the ring's screen and opens with a Core Animation
/// drop that runs in the render server, so it stays smooth while SwiftUI catches up.
///
/// The ring is on the bar only while `setShown(true)` (MenuBarPresence decides). Taking it off
/// touches nothing else: the lid lease, Stay Awake, dictation and the root helper stay with Pod Menu.
final class MenuBarController: NSObject {
    private let store: Store
    private var item: NSStatusItem?
    private let panel = PanelWindow()
    private var outsideClicks: Any?
    private var settle: DispatchWorkItem?

    init(store: Store) {
        self.store = store
        super.init()
        panel.setContent(PanelView(store: store)) { [weak self] size in self?.place(size) }
        panel.onCancel = { [weak self] in self?.close() }
        NotificationCenter.default.addObserver(
            self, selector: #selector(spaceChanged), name: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil)
        // dictation started from the panel: out of the way, the text goes to the app behind it
        NotificationCenter.default.addObserver(
            self, selector: #selector(dictationStarted), name: Dictation.startedFromPanel, object: nil)
        // `--open-panel`: open once at launch, to check the panel without clicking the menu bar
        if CommandLine.arguments.contains("--open-panel") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.open() }
        }
    }

    /// Puts the ring on the menu bar, or takes it off (closing the panel first).
    func setShown(_ shown: Bool) {
        guard shown != (item != nil) else { return }
        guard shown else {
            close()
            if let item { NSStatusBar.system.removeStatusItem(item) }
            item = nil
            return
        }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // where the user ⌘-dragged it, across a hand-over and back
        item.autosaveName = "claude-acc"
        if let button = item.button {
            let label = PassThroughHostingView(rootView: StatusLabel(store: store) { [weak item] width in
                item?.length = width
            })
            label.frame = button.bounds
            label.autoresizingMask = [.width, .height]
            button.addSubview(label)
            button.target = self
            button.action = #selector(toggle)
            button.sendAction(on: [.leftMouseDown, .rightMouseDown])
        }
        self.item = item
    }

    @objc private func toggle() {
        panel.isOpen ? close() : open()
    }

    /// Opens the panel from a URL (claude-acc://panel): already open, it stays as it is. `section`
    /// names the part to bring into view; the panel has no sections to scroll to yet, so for now
    /// every one opens the panel as it is (`services` waits for the services view).
    func showPanel(section: String?) {
        if !panel.isOpen { open() }
    }

    @objc private func dictationStarted() {
        close()
    }

    @objc private func spaceChanged() {
        if panel.isOpen { close() }
    }

    private func open() {
        place(panel.contentSize)
        item?.button?.highlight(true)
        panel.present()
        // a click anywhere else closes it, like a menu
        outsideClicks = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
            self?.close()
        }
        // live numbers start once the drop has played: their first render must not delay it
        settle?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.store.panelAppeared() }
        settle = work
        DispatchQueue.main.asyncAfter(deadline: .now() + PanelWindow.openDuration, execute: work)
    }

    private func close() {
        guard panel.isOpen else { return }
        settle?.cancel()
        if let outsideClicks { NSEvent.removeMonitor(outsideClicks) }
        outsideClicks = nil
        item?.button?.highlight(false)
        // the panel stops its clocks and spinners only once it's gone: that re-render must
        // not land in the middle of the fade
        panel.dismiss { [weak self] in self?.store.panelDisappeared() }
    }

    /// Centred on the ring's screen, right under the menu bar, never past its edges.
    private func place(_ size: CGSize) {
        // without the ring (Pod's native shell has the item), under the main screen's menu bar
        guard size.width > 0, let screen = item?.button?.window?.screen ?? NSScreen.main else { return }
        let area = screen.visibleFrame
        let width = min(size.width, area.width - 16)
        let x = (area.midX - width / 2).rounded()
        let y = area.maxY - 6 - size.height
        let card = NSRect(x: x, y: y, width: width, height: size.height)
        panel.setFrame(PanelWindow.frame(forCard: card), display: true)
    }
}

/// The menu bar label, reporting its width so the status item fits it exactly.
private struct StatusLabel: View {
    let store: Store
    let resize: (CGFloat) -> Void

    var body: some View {
        MenuBarLabel(store: store)
            .font(Font(NSFont.menuBarFont(ofSize: 0)))
            .fixedSize()
            .padding(.horizontal, 7)
            .frame(maxHeight: .infinity)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { resize($0) }
    }
}

/// Lets clicks through to the status item's button underneath.
private final class PassThroughHostingView<Content: View>: NSHostingView<Content> {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

/// A borderless panel on the system popover material with rounded corners. It can become key
/// (Escape closes it) without activating the app, so the app you work in keeps focus.
///
/// The shadow is the card layer's own, not the window's: WindowServer draws a window shadow
/// outside the layers we animate, so it popped in at full strength and lingered through the
/// fade. On the layer it fades and moves with the panel. The window is that much bigger, and
/// its transparent margin lets clicks through.
final class PanelWindow: NSPanel {
    static let openDuration = 0.34
    static let closeDuration = 0.2
    private static let radius: CGFloat = 24
    /// Room for the shadow around the card; none on top, where the menu bar is.
    static let margin = NSEdgeInsets(top: 0, left: 40, bottom: 64, right: 40)

    var onCancel: (() -> Void)?
    private(set) var contentSize: CGSize = .zero
    private(set) var isOpen = false
    private let card = ShadowCard(radius: radius)
    private let backdrop = NSVisualEffectView()

    init() {
        super.init(
            contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .popUpMenu
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        isMovable = false
        hidesOnDeactivate = false
        animationBehavior = .none
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]

        let root = MarginView()
        root.wantsLayer = true
        // a click on the shadow is a click outside the panel
        root.onClick = { [weak self] in self?.onCancel?() }
        contentView = root
        card.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(card)
        let m = Self.margin
        NSLayoutConstraint.activate([
            card.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: m.left),
            card.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -m.right),
            card.topAnchor.constraint(equalTo: root.topAnchor, constant: m.top),
            card.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -m.bottom),
        ])

        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.maskImage = Self.roundedMask(radius: Self.radius)
        backdrop.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(backdrop)
        pin(backdrop, to: card)
        card.layer?.opacity = 0
    }

    override var canBecomeKey: Bool { true }

    override func cancelOperation(_ sender: Any?) {
        onCancel?()
    }

    func setContent<Content: View>(_ content: Content, resized: @escaping (CGSize) -> Void) {
        let host = NSHostingView(rootView: content
            .onGeometryChange(for: CGSize.self) { $0.size } action: { [weak self] size in
                self?.contentSize = size
                resized(size)
            })
        host.translatesAutoresizingMaskIntoConstraints = false
        backdrop.addSubview(host)
        pin(host, to: backdrop)
        contentSize = host.fittingSize
    }

    /// The window frame that puts the card itself at `rect`.
    static func frame(forCard rect: NSRect) -> NSRect {
        NSRect(
            x: rect.minX - margin.left, y: rect.minY - margin.bottom,
            width: rect.width + margin.left + margin.right, height: rect.height + margin.top + margin.bottom)
    }

    /// Fades in and drops a few points from under the menu bar, settling with a soft spring.
    func present() {
        guard let layer = card.layer else { return }
        isOpen = true
        // pick up from wherever a close left off, so a quick reopen doesn't jump
        let from = layer.presentation() ?? layer
        let startOpacity = layer.animation(forKey: "out") == nil ? 0 : from.opacity
        let startTransform = layer.animation(forKey: "out") == nil
            ? Self.dropTransform(size: card.bounds.size, scale: 0.965, lift: 12) : from.transform
        layer.removeAllAnimations()
        layer.opacity = 1
        layer.transform = CATransform3DIdentity
        makeKeyAndOrderFront(nil)

        let drop = CASpringAnimation(perceptualDuration: Self.openDuration, bounce: 0.1)
        drop.keyPath = "transform"
        drop.fromValue = NSValue(caTransform3D: startTransform)
        drop.toValue = NSValue(caTransform3D: CATransform3DIdentity)
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = startOpacity
        fade.toValue = 1
        fade.duration = 0.18
        fade.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1, 0.36, 1)
        layer.add(drop, forKey: "in")
        layer.add(fade, forKey: "fade-in")
    }

    /// Eases back up into the menu bar while it fades, then leaves the screen; `done` runs
    /// after that, so whatever the app does next can't stall the animation.
    func dismiss(done: @escaping () -> Void) {
        guard let layer = card.layer, isOpen else { return }
        isOpen = false
        let from = layer.presentation() ?? layer
        let end = Self.dropTransform(size: card.bounds.size, scale: 0.975, lift: 8)
        layer.removeAllAnimations()
        // the model holds the end state, so nothing flashes back when the animations finish
        layer.opacity = 0
        layer.transform = end

        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            MainActor.assumeIsolated {
                // a reopen during the fade already took the panel back
                guard let self, !self.isOpen else { return }
                self.orderOut(nil)
                done()
            }
        }
        // ease-in-out on the way out: it leaves gently, then gets out of the way
        let curve = CAMediaTimingFunction(controlPoints: 0.4, 0, 0.2, 1)
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = from.opacity
        fade.toValue = 0
        fade.duration = Self.closeDuration
        fade.timingFunction = curve
        let lift = CABasicAnimation(keyPath: "transform")
        lift.fromValue = NSValue(caTransform3D: from.transform)
        lift.toValue = NSValue(caTransform3D: end)
        lift.duration = Self.closeDuration
        lift.timingFunction = curve
        layer.add(fade, forKey: "out")
        layer.add(lift, forKey: "lift")
        CATransaction.commit()
    }

    /// Scale about the top centre (AppKit layers scale about their origin, bottom left), then
    /// lift: the panel looks like it comes out of the menu bar.
    private static func dropTransform(size: CGSize, scale: CGFloat, lift: CGFloat) -> CATransform3D {
        let scaled = CATransform3DMakeScale(scale, scale, 1)
        let shift = CATransform3DMakeTranslation(
            size.width * (1 - scale) / 2, size.height * (1 - scale) + lift, 0)
        return CATransform3DConcat(scaled, shift)
    }

    private func pin(_ view: NSView, to parent: NSView) {
        NSLayoutConstraint.activate([
            view.leadingAnchor.constraint(equalTo: parent.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: parent.trailingAnchor),
            view.topAnchor.constraint(equalTo: parent.topAnchor),
            view.bottomAnchor.constraint(equalTo: parent.bottomAnchor),
        ])
    }

    private static func roundedMask(radius: CGFloat) -> NSImage {
        let edge = radius * 2 + 1
        let image = NSImage(size: NSSize(width: edge, height: edge), flipped: false) { rect in
            NSColor.black.setFill()
            NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).fill()
            return true
        }
        image.capInsets = NSEdgeInsets(top: radius, left: radius, bottom: radius, right: radius)
        image.resizingMode = .stretch
        return image
    }
}

/// The transparent margin around the card: clicks there close the panel like any click outside.
private final class MarginView: NSView {
    var onClick: (() -> Void)?

    override func mouseDown(with event: NSEvent) {
        onClick?()
    }
}

/// The card's layer carries the shadow along a rounded path, so it costs no offscreen pass
/// and fades and moves with the card.
private final class ShadowCard: NSView {
    private let radius: CGFloat

    init(radius: CGFloat) {
        self.radius = radius
        super.init(frame: .zero)
        wantsLayer = true
        layer?.masksToBounds = false
        layer?.shadowColor = NSColor.black.cgColor
        layer?.shadowOpacity = 0.42
        layer?.shadowRadius = 26
        layer?.shadowOffset = CGSize(width: 0, height: -16)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        layer?.shadowPath = CGPath(roundedRect: bounds, cornerWidth: radius, cornerHeight: radius, transform: nil)
    }
}
