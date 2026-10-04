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
struct UsageBar: View {
    let fraction: Double
    let tint: Color
    var marker: Double?
    var height: CGFloat = 7

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                if fraction > 0 {
                    Capsule()
                        .fill(tint.gradient)
                        .frame(width: max(height, geo.size.width * min(fraction, 1)))
                }
                if let marker {
                    Capsule()
                        .fill(.secondary)
                        .frame(width: 2, height: height + 5)
                        .offset(x: geo.size.width * marker - 1)
                }
            }
        }
        .frame(height: height)
        .animation(.smooth, value: fraction)
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
