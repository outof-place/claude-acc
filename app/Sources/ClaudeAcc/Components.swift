import SwiftUI

extension EnvironmentValues {
    /// One clock for the whole panel, so every "5 min ago" moves together.
    @Entry var now: Date = .now
    /// Drawing into a PNG: ImageRenderer leaves ScrollView content out, so lists render flat.
    @Entry var renderingToFile = false
    /// Continuous animations (spinning glyphs) run only while the panel is on screen.
    @Entry var animating = true
}

/// A list that scrolls inside its card in the live panel and lies flat in a PNG render.
struct CardScroll<Content: View>: View {
    @ViewBuilder let content: Content
    @Environment(\.renderingToFile) private var renderingToFile

    var body: some View {
        if renderingToFile {
            content
        } else {
            ScrollView {
                content.padding(.horizontal, 6)
            }
            .padding(.horizontal, -6)
            .scrollIndicators(.never)
            .scrollBounceBehavior(.basedOnSize)
        }
    }
}

/// The panel's own button: a soft rounded fill that lights up on hover and gives a little
/// on press; `prominent` is the violet one for the action you most likely want.
struct PanelButtonStyle: ButtonStyle {
    var prominent = false

    func makeBody(configuration: Configuration) -> some View {
        PanelButton(configuration: configuration, prominent: prominent)
    }
}

private struct PanelButton: View {
    let configuration: ButtonStyleConfiguration
    let prominent: Bool
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.controlSize) private var controlSize
    @State private var hovering = false

    var body: some View {
        let small = controlSize == .small || controlSize == .mini
        configuration.label
            .font(small ? .caption.weight(.semibold) : .callout.weight(.semibold))
            .labelStyle(.titleAndIcon)
            .lineLimit(1)
            .foregroundStyle(prominent ? AnyShapeStyle(.white) : AnyShapeStyle(.primary))
            .padding(.horizontal, small ? 10 : 13)
            .padding(.vertical, small ? 5 : 7)
            .background(fill, in: .capsule)
            .overlay {
                Capsule().strokeBorder(.white.opacity(prominent ? 0.18 : 0.07), lineWidth: 0.5)
            }
            .contentShape(.capsule)
            .scaleEffect(configuration.isPressed ? 0.96 : 1)
            .opacity(isEnabled ? 1 : 0.45)
            .onHover { hovering = $0 }
            .animation(.snappy(duration: 0.16), value: configuration.isPressed)
            .animation(.snappy(duration: 0.16), value: hovering)
    }

    private var fill: AnyShapeStyle {
        if prominent {
            return AnyShapeStyle(Format.violet.gradient.opacity(hovering ? 1 : 0.88))
        }
        return AnyShapeStyle(.white.opacity(hovering ? 0.14 : 0.08))
    }
}

extension View {
    func panelButton(prominent: Bool = false) -> some View {
        buttonStyle(PanelButtonStyle(prominent: prominent))
    }
}

/// A rounded module, like the ones in Control Center.
struct Card<Content: View, Accessory: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder let content: Content
    @ViewBuilder let accessory: Accessory

    init(
        _ title: String, symbol: String,
        @ViewBuilder content: () -> Content,
        @ViewBuilder accessory: () -> Accessory
    ) {
        self.title = title
        self.symbol = symbol
        self.content = content()
        self.accessory = accessory()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Label(title, systemImage: symbol)
                    .font(.headline)
                    .labelStyle(CardTitleLabelStyle())
                Spacer(minLength: 8)
                accessory
            }
            .frame(minHeight: 22)
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(.quinary, in: .rect(cornerRadius: 20, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(.white.opacity(0.06), lineWidth: 1)
        }
    }
}

extension Card where Accessory == EmptyView {
    init(_ title: String, symbol: String, @ViewBuilder content: () -> Content) {
        self.init(title, symbol: symbol, content: content) { EmptyView() }
    }
}

private struct CardTitleLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 7) {
            configuration.icon
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Format.violet)
                .frame(width: 18)
            configuration.title
        }
    }
}

/// A small capsule tag: tier, "Next", "Viewing"…
struct Chip: View {
    let text: String
    var symbol: String?
    var tint: Color = .secondary

    init(_ text: String, symbol: String? = nil, tint: Color = .secondary) {
        self.text = text
        self.symbol = symbol
        self.tint = tint
    }

    var body: some View {
        HStack(spacing: 3) {
            if let symbol {
                Image(systemName: symbol).imageScale(.small)
            }
            Text(text)
        }
        .font(.caption2.weight(.semibold))
        .foregroundStyle(tint)
        .lineLimit(1)
        .padding(.horizontal, 7)
        .padding(.vertical, 2.5)
        .background(tint.opacity(0.15), in: .capsule)
        .fixedSize()
    }
}

/// A small rounded switch, drawn in SwiftUI so it matches the panel (and renders to PNG).
struct PillToggleStyle: ToggleStyle {
    var tint: Color = .green

    func makeBody(configuration: Configuration) -> some View {
        PillToggle(configuration: configuration, tint: tint)
    }
}

private struct PillToggle: View {
    let configuration: ToggleStyleConfiguration
    let tint: Color
    @Environment(\.labelsVisibility) private var labels

    var body: some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 6) {
                if labels != .hidden {
                    configuration.label
                }
                Capsule()
                    .fill(configuration.isOn ? AnyShapeStyle(tint.gradient) : AnyShapeStyle(.quaternary))
                    .frame(width: 28, height: 16)
                    .overlay(alignment: configuration.isOn ? .trailing : .leading) {
                        Circle()
                            .fill(.white)
                            .padding(2)
                            .shadow(color: .black.opacity(0.25), radius: 1, y: 0.5)
                    }
                    .animation(.snappy(duration: 0.2), value: configuration.isOn)
            }
            .contentShape(.capsule)
        }
        .buttonStyle(.plain)
    }
}

extension ToggleStyle where Self == PillToggleStyle {
    static var pill: PillToggleStyle { PillToggleStyle() }
}

/// Rounded usage bar; `marker` is where auto-switch kicks in.
///
/// The fill is one animatable shape: a change redraws its path, while a frame that grows
/// would lay the card out again on every frame of the animation. It glides only when it
/// moves by a step you can see. `live` bars follow a reading taken every few seconds and
/// move in place: while any animation runs, SwiftUI updates the whole panel on every frame,
/// and gliding live readings kept it animating most of the time (40% CPU with the panel open).
struct UsageBar: View {
    let fraction: Double
    let tint: Color
    var marker: Double?
    var height: CGFloat = 7
    var live = false

    var body: some View {
        Capsule()
            .fill(.quaternary)
            .overlay {
                BarFill(fraction: fraction)
                    .fill(tint.gradient)
                    .opacity(fraction > 0 ? 1 : 0)
            }
            .overlay {
                if let marker {
                    BarMarker(at: marker, overhang: 2.5).fill(.secondary)
                }
            }
            .frame(height: height)
            .animation(live ? nil : .smooth, value: UsageBar.step(fraction))
    }

    /// Half a percent: on the widest bar about a point and a half.
    static func step(_ fraction: Double) -> Int {
        Int((min(max(fraction, 0), 1) * 200).rounded())
    }
}

/// The filled part of a usage bar: a capsule from the left edge, never narrower than round.
private nonisolated struct BarFill: Shape {
    var fraction: Double

    var animatableData: Double {
        get { fraction }
        set { fraction = newValue }
    }

    func path(in rect: CGRect) -> Path {
        let width = max(rect.height, rect.width * min(max(fraction, 0), 1))
        return Path(roundedRect: CGRect(x: rect.minX, y: rect.minY, width: width, height: rect.height),
                    cornerRadius: rect.height / 2)
    }
}

/// The auto-switch line across a usage bar, a little taller than the bar.
private nonisolated struct BarMarker: Shape {
    let at: Double
    let overhang: CGFloat

    func path(in rect: CGRect) -> Path {
        let line = CGRect(x: rect.minX + rect.width * at - 1, y: rect.minY - overhang,
                          width: 2, height: rect.height + overhang * 2)
        return Path(roundedRect: line, cornerRadius: 1)
    }
}

struct StatusDot: View {
    let color: Color
    var size: CGFloat = 8

    var body: some View {
        Circle()
            .fill(color.gradient)
            .frame(width: size, height: size)
            .shadow(color: color.opacity(0.6), radius: 3)
    }
}

/// A tinted rounded message: Orca warning, errors, results of an action.
struct Banner: View {
    let symbol: String
    let tint: Color
    let title: String
    var text: String?
    var dismiss: (() -> Void)?

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.body.weight(.semibold))
                .foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.callout.weight(.semibold))
                if let text {
                    Text(text)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            if let dismiss {
                Button("Dismiss", systemImage: "xmark", action: dismiss)
                    .labelStyle(.iconOnly)
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.12), in: .rect(cornerRadius: 16, style: .continuous))
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}

/// An SF Symbol turning clockwise. Core Animation runs the turn in the render server, so the
/// app does no work per frame, and a new speed carries on from the angle already reached.
struct TurningSymbol: NSViewRepresentable {
    let name: String
    let tint: NSColor
    var textStyle: NSFont.TextStyle = .callout
    let turnsPerSecond: Double

    func makeNSView(context: Context) -> TurningSymbolView {
        TurningSymbolView()
    }

    func updateNSView(_ view: TurningSymbolView, context: Context) {
        view.show(name, tint: tint, textStyle: textStyle)
        view.turnsPerSecond = turnsPerSecond
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: TurningSymbolView, context: Context) -> CGSize? {
        nsView.intrinsicContentSize
    }
}

final class TurningSymbolView: NSView {
    private let glyph = CALayer()
    private var image: NSImage?
    private var shown: (name: String, tint: NSColor, style: NSFont.TextStyle)?

    var turnsPerSecond = 0.0 {
        didSet { if turnsPerSecond != oldValue { retime() } }
    }

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.addSublayer(glyph)
        glyph.contentsGravity = .center
        let turn = CABasicAnimation(keyPath: "transform.rotation.z")
        turn.fromValue = 0
        turn.toValue = -2 * Double.pi  // AppKit layers count angles counterclockwise
        turn.duration = 1
        turn.repeatCount = .infinity
        turn.isRemovedOnCompletion = false
        glyph.add(turn, forKey: "turn")
        glyph.speed = 0
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override var intrinsicContentSize: NSSize { image?.size ?? .zero }

    func show(_ name: String, tint: NSColor, textStyle: NSFont.TextStyle) {
        if let shown, shown.name == name, shown.tint == tint, shown.style == textStyle { return }
        shown = (name, tint, textStyle)
        let config = NSImage.SymbolConfiguration(textStyle: textStyle)
            .applying(NSImage.SymbolConfiguration(paletteColors: [tint]))
        image = NSImage(systemSymbolName: name, accessibilityDescription: nil)?.withSymbolConfiguration(config)
        glyph.contents = image
        invalidateIntrinsicContentSize()
        needsLayout = true
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        glyph.bounds = CGRect(origin: .zero, size: bounds.size)
        glyph.position = CGPoint(x: bounds.midX, y: bounds.midY)
        CATransaction.commit()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        glyph.contentsScale = window?.backingScaleFactor ?? 2
    }

    /// Freezes the layer's clock at the angle it reached, then runs it on from there at the
    /// new rate; zero holds the glyph still.
    private func retime() {
        let now = CACurrentMediaTime()
        glyph.timeOffset = glyph.convertTime(now, from: nil)
        glyph.beginTime = now
        glyph.speed = Float(turnsPerSecond)
    }
}
