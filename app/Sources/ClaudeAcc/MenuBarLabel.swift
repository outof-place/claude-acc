import SwiftUI

/// Menu bar ring with a percentage: usage of the active account's window that runs out first.
/// Badge: orange when an account needs signing in, red when the Mac runs out of memory.
struct MenuBarLabel: View {
    let store: Store

    var body: some View {
        let used = store.snapshot?.active?.worstUsed
        HStack(spacing: 4) {
            Image(nsImage: RingImage.make(
                fraction: used.map { $0 / 100 },
                color: Format.nsTint(used),
                badge: badge))
            Text(text(used: used))
                .monospacedDigit()
            if store.awake.isOn {
                // Stay Awake is holding the Mac up, like Amphetamine's pill
                Image(systemName: "cup.and.heat.waves.fill")
            }
            if let hot {
                // only when it matters: the bar stays clean below 90 °C
                Text("\(Int(hot.rounded()))°")
                    .monospacedDigit()
                    .foregroundStyle(hot >= 95 ? .red : .orange)
            }
        }
    }

    /// Hottest CPU/GPU sensor from the fan daemon, when it's fresh and at 90 °C or more.
    private var hot: Double? {
        guard let state = store.fanState, Date.now.timeIntervalSince1970 - state.at < 15 else { return nil }
        let value = [state.cpu, state.gpu].compactMap(\.self).max() ?? 0
        return value >= 90 ? value : nil
    }

    private var badge: NSColor? {
        if store.guardState?.snapshot?.pressure.level == 2 { return .systemRed }
        return store.snapshot?.anyNeedsLogin == true ? .systemOrange : nil
    }

    private func text(used: Double?) -> String {
        guard let snapshot = store.snapshot else { return store.problem == nil ? "…" : "!" }
        return snapshot.foreignRuntime ? "?" : Format.percent(used)
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
