//
//  DesignSystem.swift
//  HealthTracker
//
//  The visual language, in one place: a small semantic palette (validated with the
//  data-viz colour method, light + dark), the rule that turns a target + an amount
//  into a colour/state, and the two primitives every screen is built from — a Ring
//  (the few headline metrics) and a TargetBar (the many nutrients).
//

import SwiftUI

// MARK: - Colour

extension Color {
    /// A fixed colour from a 0xRRGGBB literal (used for the never-themed status hues).
    init(hex: UInt) {
        self = Color(uiColor: UIColor(hex: hex))
    }

    /// A colour that resolves differently in light and dark. Both values are chosen
    /// deliberately (dark is not an auto-flip) per the data-viz palette method.
    init(lightHex: UInt, darkHex: UInt) {
        self = Color(uiColor: UIColor { trait in
            UIColor(hex: trait.userInterfaceStyle == .dark ? darkHex : lightHex)
        })
    }
}

extension UIColor {
    convenience init(hex: UInt) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: 1)
    }
}

enum Palette {
    // Status — the validated data-viz status palette. Fixed (not themed): a status
    // colour must mean the same thing everywhere, and it is always paired with a
    // symbol + text so meaning never rides on colour alone.
    static let good = Color(hex: 0x0CA30C)
    static let warning = Color(hex: 0xFAB219)
    static let critical = Color(hex: 0xD03B3B)

    // Ink variants for text/labels, where the fill colours can dip below text
    // contrast on a light surface.
    static let goodText = Color(lightHex: 0x0A7A0A, darkHex: 0x37C74B)
    static let warningText = Color(lightHex: 0x8A5A00, darkHex: 0xF0B84A)
    static let criticalText = Color(lightHex: 0xB92C2C, darkHex: 0xE86A6A)

    // Neutral / accent adapt to the mode.
    static let neutral = Color(lightHex: 0x9A9A9E, darkHex: 0x7C7C82)
    static let accent = Color(lightHex: 0x2A78D6, darkHex: 0x3987E5)
    static let accentText = Color(lightHex: 0x2168C0, darkHex: 0x5AA0EE)

    // Macros. Protein is the hero (muscle) — a distinct, confident indigo.
    static let protein = Color(lightHex: 0x5B5BD6, darkHex: 0x8B89F2)
    static let carbs = Color(lightHex: 0x0E9E9A, darkHex: 0x35C7C1)
    static let fat = Color(lightHex: 0xE58A2B, darkHex: 0xF2A63B)

    // Recomposition chart series (validated as a set, light + dark).
    static let muscle = Color(lightHex: 0x1BAF7A, darkHex: 0x2ECB92)
    static let weight = Color(lightHex: 0x2A78D6, darkHex: 0x3987E5)
    static let bodyFat = Color(lightHex: 0xEB6834, darkHex: 0xF0824E)

    // Surfaces — native grouped look.
    static let screen = Color(uiColor: .systemGroupedBackground)
    static let card = Color(uiColor: .secondarySystemGroupedBackground)
    static let track = Color.primary.opacity(0.08)
}

// MARK: - Target → state

/// Everything the UI needs to render one metric against its target: how full the
/// ring/bar is, the colour, an ink for text, a status symbol, and whether it counts
/// as "on target" for the summary tally.
struct MetricStatus {
    var fraction: Double     // consumed / goal (may exceed 1; views clamp the fill)
    var fill: Color
    var text: Color
    var symbol: String       // "" for none
    var onTarget: Bool

    /// Resolve a target + a consumed amount into a status, per the target's kind:
    ///  - reach: fill toward the floor; met is green, close is amber, far is grey;
    ///  - limit: fill toward the ceiling; comfortable is green, near is amber, over red;
    ///  - window: fill toward the mid-point; inside is green, under is "keep going"
    ///    (accent, not a failure), over the ceiling is red.
    static func of(_ target: Target, consumed: Double) -> MetricStatus {
        switch target.kind {
        case Target.Kind.limit:
            let ceiling = target.ceiling ?? target.goal
            let frac = ceiling > 0 ? consumed / ceiling : 0
            if consumed > ceiling {
                return .init(fraction: frac, fill: Palette.critical,
                             text: Palette.criticalText,
                             symbol: "exclamationmark.circle.fill", onTarget: false)
            }
            if frac >= 0.8 {
                return .init(fraction: frac, fill: Palette.warning,
                             text: Palette.warningText,
                             symbol: "exclamationmark.triangle.fill", onTarget: true)
            }
            return .init(fraction: frac, fill: Palette.good, text: Palette.goodText,
                         symbol: "checkmark.circle.fill", onTarget: true)

        case Target.Kind.window:
            let floor = target.floor ?? 0
            let ceiling = target.ceiling ?? target.goal
            let frac = target.goal > 0 ? consumed / target.goal : 0
            if consumed > ceiling {
                return .init(fraction: ceiling > 0 ? consumed / ceiling : 1,
                             fill: Palette.critical, text: Palette.criticalText,
                             symbol: "exclamationmark.circle.fill", onTarget: false)
            }
            if consumed >= floor {
                return .init(fraction: frac, fill: Palette.good, text: Palette.goodText,
                             symbol: "checkmark.circle.fill", onTarget: true)
            }
            return .init(fraction: frac, fill: Palette.accent, text: Palette.accentText,
                         symbol: "arrow.up.circle.fill", onTarget: false)

        default: // reach
            let floor = target.floor ?? target.goal
            let frac = floor > 0 ? consumed / floor : 0
            if consumed >= floor {
                return .init(fraction: frac, fill: Palette.good, text: Palette.goodText,
                             symbol: "checkmark.circle.fill", onTarget: true)
            }
            if frac >= 0.6 {
                return .init(fraction: frac, fill: Palette.warning,
                             text: Palette.warningText, symbol: "hourglass", onTarget: false)
            }
            return .init(fraction: frac, fill: Palette.neutral, text: .secondary,
                         symbol: "", onTarget: false)
        }
    }
}

// MARK: - Ring

/// A circular progress ring with a rounded cap and any centred content. The fill is
/// clamped to a full turn; overshoot is signalled by colour (the status goes red),
/// never by a ring that wraps past itself.
struct Ring<Content: View>: View {
    var progress: Double
    var color: Color
    var lineWidth: CGFloat = 12
    var content: Content

    init(progress: Double, color: Color, lineWidth: CGFloat = 12,
         @ViewBuilder content: () -> Content = { EmptyView() }) {
        self.progress = progress
        self.color = color
        self.lineWidth = lineWidth
        self.content = content()
    }

    var body: some View {
        ZStack {
            Circle().stroke(Palette.track, lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: max(0, min(progress, 1)))
                .stroke(color, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.easeOut(duration: 0.55), value: progress)
            content
        }
    }
}

// MARK: - TargetBar

/// A horizontal progress bar for a nutrient. The fill is clamped to the track; an
/// optional marker draws the target line (used to show a window's lower edge).
struct TargetBar: View {
    var fraction: Double
    var fill: Color
    var height: CGFloat = 10
    var markerFraction: Double? = nil

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.track)
                Capsule()
                    .fill(fill)
                    .frame(width: max(height, min(max(fraction, 0), 1) * w))
                    .animation(.easeOut(duration: 0.5), value: fraction)
                if let marker = markerFraction, marker > 0, marker < 1 {
                    Rectangle()
                        .fill(Color.primary.opacity(0.35))
                        .frame(width: 2, height: height + 4)
                        .offset(x: marker * w - 1)
                }
            }
        }
        .frame(height: height)
    }
}

// MARK: - Small building blocks

/// A section header with an icon, used above card groups.
struct SectionHeader: View {
    var title: String
    var systemImage: String
    var accent: Color = .secondary

    var body: some View {
        Label {
            Text(title).font(.headline)
        } icon: {
            Image(systemName: systemImage).foregroundStyle(accent)
        }
        .padding(.horizontal, 4)
    }
}

/// An up/down delta with a colour that respects whether up is good.
struct DeltaBadge: View {
    var delta: Double
    var unit: String
    var upIsGood: Bool
    var decimals: Int = 1

    private var isFlat: Bool { abs(delta) < pow(10, -Double(decimals)) / 2 }

    var body: some View {
        let good = delta > 0 ? upIsGood : !upIsGood
        let color = isFlat ? Color.secondary : (good ? Palette.goodText : Palette.criticalText)
        let symbol = isFlat ? "equal" : (delta > 0 ? "arrow.up.right" : "arrow.down.right")
        return Label {
            Text(delta.formatted(.number.precision(.fractionLength(decimals)).sign(strategy: .never))
                 + (unit.isEmpty ? "" : " \(unit)"))
        } icon: {
            Image(systemName: symbol)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(color)
        .labelStyle(.titleAndIcon)
    }
}

extension View {
    /// Wrap a view as a card on the grouped background.
    func card(padding: CGFloat = 16) -> some View {
        self
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
    }
}

// MARK: - FlowLayout

/// A simple wrapping layout: lays children left-to-right and wraps to the next line
/// when they run out of width. Used for the food-source chips on the nutrient
/// detail screen.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > maxWidth, x > 0 {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(.unspecified)
            if x + size.width > bounds.maxX, x > bounds.minX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            view.place(at: CGPoint(x: x, y: y), anchor: .topLeading, proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}
