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

// MARK: - NutrientReading (the kinetics-aware read)

/// Reads one micronutrient the way its biology demands, replacing the single-day
/// `MetricStatus` for the Nutrients screen. It combines two independent stories:
///
///   * a DEFICIT story — is the floor met? Judged on the rolling AVERAGE for a
///     buffered ("rolling") nutrient (so a single low day is covered by reserves and
///     never turns red), and on TODAY for a non-cumulative one (where consistency is
///     what matters);
///   * a CEILING story — for a nutrient with a reachable toxicity limit (a UL) or a
///     dietary limit, how close is intake to it? Judged on the worse of today (acute)
///     and the rolling average (chronic), so one liver-heavy day still flags.
///
/// The headline colour is the worse of the two, so "reserves fine" never hides
/// "approaching a toxic ceiling", and a harmless surplus (no UL) never reads as risk.
struct NutrientReading {
    let target: Target
    let today: Double
    /// This nutrient's intake per completed day in the rolling window, oldest first.
    let dailyHistory: [Double]
    /// When true, the DEFICIT is read against today even for a buffered nutrient — the
    /// "Hoje" lens ("am I on track for today's target right now?"). The ceiling/safety
    /// read stays chronic regardless, so the lens can never hide a toxicity risk.
    let readToday: Bool

    init(target: Target, today: Double, dailyHistory: [Double], readToday: Bool = false) {
        self.target = target
        self.today = today
        self.dailyHistory = dailyHistory
        self.readToday = readToday
    }

    /// Pull a nutrient's today + rolling history straight out of a /today payload.
    init(key: String, target: Target, response: TodayResponse, readToday: Bool = false) {
        self.init(target: target, today: response.consumed(key),
                  dailyHistory: response.historyDays.map { $0.consumed(key) },
                  readToday: readToday)
    }

    // -- the window --
    var daysCounted: Int { dailyHistory.count }
    var average: Double? {
        daysCounted > 0 ? dailyHistory.reduce(0, +) / Double(daysCounted) : nil
    }
    /// A buffered nutrient's reserves (its rolling average), or today when it isn't
    /// buffered or has no history yet. Independent of the lens.
    var chronicBasis: Double { (target.isRolling ? average : nil) ?? today }
    /// True when the deficit is read against reserves (the biological lens on a rolling
    /// nutrient with history), not today.
    var usesRolling: Bool { !readToday && target.isRolling && average != nil }
    /// The amount the deficit is judged against: reserves for a buffered nutrient in the
    /// biological lens, otherwise today.
    var basis: Double { readToday ? today : chronicBasis }

    var isLimit: Bool { target.kind == Target.Kind.limit }
    /// The floor to reach (nil for a pure limit — there's nothing to hit).
    var floor: Double? { isLimit ? nil : target.floor }
    /// The toxicity upper limit, or nil when a surplus is biologically safe.
    var upperLimit: Double? { target.upperLimit }

    var floorFraction: Double {
        guard let f = floor, f > 0 else { return 0 }
        return basis / f
    }
    /// The exposure tested against the ceiling — ALWAYS chronic-aware (the worse of
    /// today and the rolling average), so the "Hoje" lens can never hide a chronic
    /// toxicity. A pure limit is judged on today (a daily budget).
    var ceilingExposure: Double { isLimit ? today : max(today, chronicBasis) }
    var ceilingFraction: Double {
        guard let c = target.ceiling, c > 0 else { return 0 }
        return ceilingExposure / c
    }

    enum Deficit { case met, close, far }
    enum Ceiling { case none, ok, near, over }

    var deficit: Deficit {
        guard floor != nil else { return .met }          // no floor -> nothing to reach
        if floorFraction >= 1 { return .met }
        return floorFraction >= 0.6 ? .close : .far
    }
    var ceiling: Ceiling {
        guard target.ceiling != nil else { return .none }
        if ceilingFraction > 1 { return .over }
        if ceilingFraction >= 0.8 { return .near }
        return .ok
    }

    /// The headline colour: the worse of the ceiling and deficit stories.
    var fill: Color {
        switch ceiling {
        case .over: return Palette.critical
        case .near: return Palette.warning              // a warning even if the floor is met
        case .ok, .none:
            switch deficit {
            case .met:   return Palette.good
            case .close: return Palette.warning
            case .far:   return Palette.neutral
            }
        }
    }

    var textColor: Color {
        switch ceiling {
        case .over: return Palette.criticalText
        case .near: return Palette.warningText
        case .ok, .none:
            switch deficit {
            case .met:   return Palette.goodText
            case .close: return Palette.warningText
            case .far:   return .secondary
            }
        }
    }

    var symbol: String {
        switch ceiling {
        case .over: return "exclamationmark.circle.fill"
        case .near: return "exclamationmark.triangle.fill"
        case .ok, .none:
            switch deficit {
            case .met:   return "checkmark.circle.fill"
            case .close: return usesRolling ? "arrow.up.circle.fill" : "hourglass"
            case .far:   return usesRolling ? "arrow.down.circle" : ""
            }
        }
    }

    /// A short pt-PT status word, tuned to the nutrient's kind so the same colour reads
    /// correctly whether it's a reserve, a daily essential, or a limit.
    var label: String {
        if isLimit {
            switch ceiling {
            case .over: return "acima"
            case .near: return "perto do limite"
            default:    return "com folga"
            }
        }
        switch ceiling {
        case .over: return "acima do teto"
        case .near: return "perto do teto"
        case .ok, .none:
            switch deficit {
            case .met:   return "no alvo"
            case .close: return usesRolling ? "reservas a descer" : "quase lá"
            case .far:   return usesRolling ? "reservas baixas" : "em falta"
            }
        }
    }

    /// Counts toward the section's "on target" tally: floor met (or a limit respected)
    /// and not over a ceiling.
    var onTarget: Bool { ceiling != .over && (isLimit || deficit == .met) }
    var isNearCeiling: Bool { ceiling == .near }
    var isOverCeiling: Bool { ceiling == .over }
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

// MARK: - Kinetics lenses

/// The consistency record for a NON-CUMULATIVE nutrient: one dot per completed day,
/// filled green when that day hit the floor, hollow when it fell short. The whole
/// point of the daily lens — for a nutrient the body can't store, what matters isn't a
/// single big day, it's how many days in a row you actually hit it.
struct WeekDots: View {
    var values: [Double]        // this nutrient, per completed day, oldest first
    var floor: Double
    var dot: CGFloat = 8

    var body: some View {
        HStack(spacing: 5) {
            ForEach(Array(values.enumerated()), id: \.offset) { _, v in
                let met = floor > 0 && v >= floor
                Circle()
                    .fill(met ? Palette.good : Palette.track)
                    .frame(width: dot, height: dot)
                    .overlay(
                        Circle().strokeBorder(
                            met ? Color.clear : Palette.neutral.opacity(0.45),
                            lineWidth: 1))
            }
        }
    }
}

/// The reserves view for a CUMULATIVE nutrient: the bar fills to the rolling AVERAGE
/// (coloured by the average's status, so a low today reads calm), and a faint marker
/// shows where today alone landed — the single-day dip or spike against the buffer.
struct RollingBar: View {
    var averageFraction: Double     // average / floor
    var todayFraction: Double       // today / floor
    var fill: Color
    var height: CGFloat = 10

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let avg = min(max(averageFraction, 0), 1)
            let today = min(max(todayFraction, 0), 1)
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.track)
                Capsule().fill(fill)
                    .frame(width: max(height, avg * w))
                    .animation(.easeOut(duration: 0.5), value: averageFraction)
                Rectangle()
                    .fill(Color.primary.opacity(0.35))
                    .frame(width: 2, height: height + 4)
                    .offset(x: today * w - 1)
            }
        }
        .frame(height: height)
    }
}

/// The floor -> optimal -> ceiling view for a nutrient with a real toxicity limit.
/// The track is banded: a faint deficit zone up to the floor, a green optimal band
/// from the floor to 80% of the UL, and a red zone approaching and past the UL — with
/// a marker for where intake actually sits. This is what makes a safe surplus look
/// calm and a dangerous one look dangerous, instead of both reading as "over 100%".
struct RangeGauge: View {
    var floor: Double?
    var ceiling: Double         // the UL
    var current: Double
    var marker: Color
    var height: CGFloat = 12

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let floorX = CGFloat((floor.map { min(max($0 / ceiling, 0), 1) } ?? 0)) * w
            let warnX = 0.8 * w                                   // 80% of the UL
            let curX = CGFloat(min(max(current / ceiling, 0), 1)) * w
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.track)                    // deficit zone (0..floor)
                Capsule().fill(Palette.good.opacity(0.22))       // optimal band
                    .frame(width: max(0, warnX - floorX)).offset(x: floorX)
                Capsule().fill(Palette.critical.opacity(0.20))   // near/over the UL
                    .frame(width: max(0, w - warnX)).offset(x: warnX)
                Capsule().fill(marker)                           // where intake sits
                    .frame(width: 3, height: height + 6)
                    .offset(x: min(max(curX, 1.5), w - 1.5) - 1.5)
                    .animation(.easeOut(duration: 0.5), value: current)
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
