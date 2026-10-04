import AppKit
import SwiftUI

/// The ring in the menu bar and the panel under it. MenuBarExtra can neither place its window
/// nor animate it, and a panel wider than the room left of the ring ran off the screen. This
/// one is centred under the menu bar of the ring's screen and opens with a Core Animation
/// drop that runs in the render server, so it stays smooth while SwiftUI catches up.
final class MenuBarController: NSObject {
    private let store: Store
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let panel = PanelWindow()
    private var outsideClicks: Any?
    private var settle: DispatchWorkItem?

    init(store: Store) {
        self.store = store
        super.init()
        if let button = item.button {
            let label = PassThroughHostingView(rootView: StatusLabel(store: store) { [weak self] width in
                self?.item.length = width
            })
            label.frame = button.bounds
            label.autoresizingMask = [.width, .height]
            button.addSubview(label)
            button.target = self
            button.action = #selector(toggle)
            button.sendAction(on: [.leftMouseDown, .rightMouseDown])
        }
        panel.setContent(PanelView(store: store)) { [weak self] size in self?.place(size) }
        panel.onCancel = { [weak self] in self?.close() }
        NotificationCenter.default.addObserver(
            self, selector: #selector(spaceChanged), name: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil)
        // `--open-panel`: open once at launch, to check the panel without clicking the menu bar
        if CommandLine.arguments.contains("--open-panel") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in self?.open() }
        }
    }

    @objc private func toggle() {
        panel.isVisible ? close() : open()
    }

    @objc private func spaceChanged() {
        if panel.isVisible { close() }
    }

    private func open() {
        place(panel.contentSize)
        item.button?.highlight(true)
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
        guard panel.isVisible else { return }
        settle?.cancel()
        if let outsideClicks { NSEvent.removeMonitor(outsideClicks) }
        outsideClicks = nil
        item.button?.highlight(false)
        panel.dismiss()
        store.panelDisappeared()
    }

    /// Centred on the ring's screen, right under the menu bar, never past its edges.
    private func place(_ size: CGSize) {
        guard size.width > 0, let screen = item.button?.window?.screen ?? NSScreen.main else { return }
        let area = screen.visibleFrame
        let width = min(size.width, area.width - 16)
        let x = (area.midX - width / 2).rounded()
        let y = area.maxY - 6 - size.height
        panel.setFrame(NSRect(x: x, y: y, width: width, height: size.height), display: true)
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
final class PanelWindow: NSPanel {
    static let openDuration = 0.32
    private static let radius: CGFloat = 24

    var onCancel: (() -> Void)?
    private(set) var contentSize: CGSize = .zero
    private let backdrop = NSVisualEffectView()

    init() {
        super.init(
            contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .popUpMenu
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        isMovable = false
        hidesOnDeactivate = false
        animationBehavior = .none
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient, .ignoresCycle]

        backdrop.material = .popover
        backdrop.blendingMode = .behindWindow
        backdrop.state = .active
        backdrop.maskImage = Self.roundedMask(radius: Self.radius)
        backdrop.wantsLayer = true
        contentView = backdrop
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
                self?.invalidateShadow()
            })
        host.translatesAutoresizingMaskIntoConstraints = false
        backdrop.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: backdrop.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: backdrop.trailingAnchor),
            host.topAnchor.constraint(equalTo: backdrop.topAnchor),
            host.bottomAnchor.constraint(equalTo: backdrop.bottomAnchor),
        ])
        contentSize = host.fittingSize
    }

    /// Fades in and drops a few points from under the menu bar, settling with a soft spring.
    func present() {
        guard let layer = backdrop.layer else {
            makeKeyAndOrderFront(nil)
            return
        }
        layer.removeAllAnimations()
        alphaValue = 1
        makeKeyAndOrderFront(nil)

        let size = backdrop.bounds.size
        let start = Self.dropTransform(size: size, scale: 0.97, lift: 10)
        let drop = CASpringAnimation(perceptualDuration: Self.openDuration, bounce: 0.12)
        drop.keyPath = "transform"
        drop.fromValue = NSValue(caTransform3D: start)
        drop.toValue = NSValue(caTransform3D: CATransform3DIdentity)
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0
        fade.toValue = 1
        fade.duration = 0.16
        fade.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1, 0.36, 1)
        layer.add(drop, forKey: "drop")
        layer.add(fade, forKey: "fade")
    }

    /// A quick fade with a slight lift, then out of the way.
    func dismiss() {
        guard let layer = backdrop.layer else {
            orderOut(nil)
            return
        }
        CATransaction.begin()
        CATransaction.setCompletionBlock { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                // a reopen during the fade already took the panel back
                if layer.animation(forKey: "drop") == nil { self.orderOut(nil) }
            }
        }
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 1
        fade.toValue = 0
        fade.duration = 0.12
        fade.timingFunction = CAMediaTimingFunction(name: .easeIn)
        fade.fillMode = .forwards
        fade.isRemovedOnCompletion = false
        let lift = CABasicAnimation(keyPath: "transform")
        lift.toValue = NSValue(caTransform3D: Self.dropTransform(size: backdrop.bounds.size, scale: 0.985, lift: 6))
        lift.duration = 0.12
        lift.timingFunction = CAMediaTimingFunction(name: .easeIn)
        lift.fillMode = .forwards
        lift.isRemovedOnCompletion = false
        layer.removeAnimation(forKey: "drop")
        layer.add(fade, forKey: "out")
        layer.add(lift, forKey: "lift")
        CATransaction.commit()
    }

    override func orderOut(_ sender: Any?) {
        super.orderOut(sender)
        backdrop.layer?.removeAnimation(forKey: "out")
        backdrop.layer?.removeAnimation(forKey: "lift")
    }

    /// Scale about the top centre (AppKit layers scale about their origin, bottom left), then
    /// lift: the panel looks like it comes out of the menu bar.
    private static func dropTransform(size: CGSize, scale: CGFloat, lift: CGFloat) -> CATransform3D {
        let scaled = CATransform3DMakeScale(scale, scale, 1)
        let shift = CATransform3DMakeTranslation(
            size.width * (1 - scale) / 2, size.height * (1 - scale) + lift, 0)
        return CATransform3DConcat(scaled, shift)
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
