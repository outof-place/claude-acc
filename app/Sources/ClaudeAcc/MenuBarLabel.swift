import SwiftUI

/// Menu bar ring with a percentage: usage of the active account's window that runs out first.
/// Badge: orange when an account needs signing in, red when the Mac runs out of memory.
struct MenuBarLabel: View {
    let store: Store

    var body: some View {
        let label = store.label
        let badge: NSColor? = label.badge.map { $0 == .memory ? .systemRed : .systemOrange }
        HStack(spacing: 4) {
            // Pod Menu without quota data (signed out, no accounts, loading): Pod's mark instead of
            // an empty ring; with numbers the ring stays, it is the useful part. One item either way
            if label.showsPodMark(in: PodMenu.active), let mark = PodMark.make(badge: badge) {
                Image(nsImage: mark)
            } else {
                Image(nsImage: RingImage.make(
                    fraction: label.used.map { $0 / 100 },
                    color: Format.nsTint(label.used),
                    badge: badge))
            }
            Text(label.text)
                .monospacedDigit()
            if store.awake.isOn {
                // Stay Awake is holding the Mac up, like Amphetamine's pill
                Image(systemName: "cup.and.heat.waves.fill")
            }
            if let hot = label.hot {
                // only when it matters: the bar stays clean below 90 °C
                Text("\(hot)°")
                    .monospacedDigit()
                    .foregroundStyle(hot >= 95 ? .red : .orange)
            }
        }
    }
}

/// The label's numbers: the active account's usage of the window that runs out first, the
/// badge (red: the Mac runs out of memory, orange: an account needs signing in) and the hottest
/// CPU/GPU sensor when the fan daemon's reading is fresh and at 90 °C or more.
struct MenuLabelState: Equatable {
    enum Badge: Equatable { case login, memory }

    var used: Double?
    var text = "…"
    var badge: Badge?
    var hot: Int?

    /// Pod's mark replaces the ring only in Pod Menu, and only while there is no usage to draw.
    func showsPodMark(in podMenu: Bool) -> Bool {
        podMenu && used == nil
    }
}

/// Pod's menu bar template (resources/brand/native/Assets.xcassets/MenuBarIcon in outof-place/pod,
/// branch pod/brand 625f2d5b61): one path, drawn by AppKit from the SVG.
enum PodMark {
    static let svg = ##"<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><path fill="#000" fill-rule="evenodd" d="M0 15.45C3.023 3.677 5.903 1.66 18 2.846 13.591 12.64 10.711 14.657 0 15.45ZM4.932 9.478A1.714 1.714 0 1 0 4.932 12.906A1.714 1.714 0 1 0 4.932 9.478ZM8.622 6.894A1.714 1.714 0 1 0 8.622 10.322A1.714 1.714 0 1 0 8.622 6.894ZM12.312 4.311A1.714 1.714 0 1 0 12.312 7.738A1.714 1.714 0 1 0 12.312 4.311Z"/></svg>"##

    static let template: NSImage? = {
        guard let image = NSImage(data: Data(svg.utf8)) else { return nil }
        image.size = NSSize(width: 16, height: 16)
        image.isTemplate = true
        return image
    }()

    /// The template as is, or with the ring's badge dot: then tinted by hand in the label colour,
    /// because a coloured dot can't live in a template image.
    static func make(badge: NSColor?) -> NSImage? {
        guard let base = template else { return nil }
        guard let badge else { return base }
        let image = NSImage(size: base.size, flipped: false) { rect in
            base.draw(in: rect)
            NSColor.labelColor.set()
            rect.fill(using: .sourceAtop)
            badge.setFill()
            NSBezierPath(ovalIn: NSRect(x: rect.maxX - 6, y: rect.maxY - 6, width: 6, height: 6)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }
}

enum RingImage {
    static func make(fraction: Double?, color: NSColor, badge: NSColor?) -> NSImage {
        let image = NSImage(size: NSSize(width: 16, height: 16), flipped: false) { rect in
            let line: CGFloat = 2.2
            let ring = rect.insetBy(dx: line / 2 + 1, dy: line / 2 + 1)
            let center = NSPoint(x: ring.midX, y: ring.midY)

            let track = NSBezierPath(ovalIn: ring)
            track.lineWidth = line
            NSColor.labelColor.withAlphaComponent(0.25).setStroke()
            track.stroke()

            if let fraction, fraction > 0 {
                let arc = NSBezierPath()
                arc.appendArc(
                    withCenter: center, radius: ring.width / 2, startAngle: 90,
                    endAngle: 90 - 360 * min(fraction, 1), clockwise: true)
                arc.lineWidth = line
                arc.lineCapStyle = .round
                color.setStroke()
                arc.stroke()
            }

            if let badge {
                badge.setFill()
                NSBezierPath(ovalIn: NSRect(x: rect.maxX - 6, y: rect.maxY - 6, width: 6, height: 6)).fill()
            }
            return true
        }
        image.isTemplate = false
        return image
    }
}
