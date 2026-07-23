//
//  InsightsView.swift
//  HealthTracker
//
//  The coach tab: the Sunday weekly review (continuity → headline → wins → the ONE
//  focus → a swap) and an always-available next-meal sheet with three ranked plates.
//  Every number was computed deterministically in the backend and every sentence was
//  written by the strong model on the Mac from those facts — this view only renders
//  what the coach decided, in the app's own visual language.
//

import SwiftUI
import UIKit

// MARK: - Store

@MainActor
@Observable
final class InsightsStore {
    var weekly: WeeklyInsightsResponse?
    var nextMeal: NextMealResponse?
    var errorMessage: String?
    var isLoading = false
    var isRefreshing = false

    init() {
        weekly = APIClient.shared.cachedWeeklyInsights()
        nextMeal = APIClient.shared.cachedNextMeal()
    }

    func load() async {
        let had = weekly != nil || nextMeal != nil
        if had { isRefreshing = true } else { isLoading = true }
        defer { isLoading = false; isRefreshing = false }

        // The weekly review is the primary payload; the next-meal cache is best-effort
        // and must never block or error the review (the Mac may not have run today).
        async let weeklyResult = APIClient.shared.weeklyInsights()
        async let mealResult = APIClient.shared.nextMeal()
        do {
            weekly = try await weeklyResult
            errorMessage = nil
        } catch {
            if !had { errorMessage = error.localizedDescription }
        }
        if let meal = try? await mealResult { nextMeal = meal }
    }
}

// MARK: - Screen

struct InsightsView: View {
    let store: InsightsStore
    @State private var showPlates = false

    var body: some View {
        NavigationStack {
            Group {
                if let weekly = store.weekly, weekly.isReady, let report = weekly.report {
                    review(report: report, weekly: weekly)
                } else if store.isLoading || store.errorMessage != nil {
                    LoadingOrError(isLoading: store.isLoading, error: store.errorMessage) {
                        Task { await store.load() }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    pendingReview
                }
            }
            .background(Palette.screen)
            .navigationTitle("Coach")
            .toolbar {
                if store.isRefreshing {
                    ToolbarItem(placement: .topBarTrailing) { SyncIndicator() }
                }
            }
            .refreshable { await store.load() }
            .sheet(isPresented: $showPlates) {
                NextMealSheet(response: store.nextMeal)
            }
        }
        .task { await store.load() }
    }

    // MARK: the weekly review

    private func review(report: WeeklyReport, weekly: WeeklyInsightsResponse) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                if let range = weekRange(weekly) {
                    Text(range)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 4)
                }

                if weekly.priorFocusDelta != nil || report.continuity != nil {
                    ContinuityPill(delta: weekly.priorFocusDelta, sentence: report.continuity)
                }

                HeadlineCard(text: report.headline)

                NextMealButton(response: store.nextMeal) { showPlates = true }

                if !report.wins.isEmpty {
                    WinsCard(wins: report.wins)
                }

                FocusCard(focus: report.focus)

                if let swap = report.swap {
                    SwapCard(swap: swap)
                }

                if let note = report.encouragement, !note.isEmpty {
                    EncouragementFooter(text: note)
                }

                if let cov = weekly.coverageNote, !cov.isEmpty, cov.contains("insuficientes") {
                    Text(cov)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 6)
                }
            }
            .padding(16)
        }
    }

    private var pendingReview: some View {
        ContentUnavailableView {
            Label("O teu resumo está a chegar", systemImage: "sparkles")
        } description: {
            Text("Todos os domingos preparo uma análise da tua semana — o que está a "
                 + "bombar e o próximo passo. Volta cá no domingo.")
        } actions: {
            if store.nextMeal?.isReady == true {
                Button {
                    showPlates = true
                } label: {
                    Label("Ver ideias para hoje", systemImage: "fork.knife")
                }
                .buttonStyle(.borderedProminent)
            }
        }
    }

    private func weekRange(_ w: WeeklyInsightsResponse) -> String? {
        guard let start = w.windowStart, let end = w.windowEnd else { return nil }
        return "Semana de \(shortDate(start)) a \(shortDate(end))"
    }
}

// MARK: - Continuity strip

/// The retention mechanism made visible: how the focus the LAST report set has moved.
/// A coach that remembers is a relationship, not a report.
struct ContinuityPill: View {
    let delta: ContinuityDelta?
    let sentence: String?

    var body: some View {
        let good = delta?.towardTarget ?? true
        let flat = (delta?.direction ?? "flat") == "flat"
        let color: Color = flat ? Palette.neutral : (good ? Palette.goodText : Palette.criticalText)
        let symbol = flat ? "equal.circle.fill"
            : ((delta?.direction == "up") ? "arrow.up.right.circle.fill" : "arrow.down.right.circle.fill")
        return HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.title3)
                .foregroundStyle(color)
            Text(sentence ?? fallbackText)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 12)
        .padding(.horizontal, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var fallbackText: String {
        guard let d = delta else { return "Desde a semana passada" }
        let label = NutrientCatalog.byKey[d.key]?.label ?? d.key
        let verb = d.direction == "up" ? "subiu" : (d.direction == "down" ? "desceu" : "manteve-se")
        let mag = abs(d.pct) >= 1 ? " \(abs(Int(d.pct)))%" : ""
        return "\(label) \(verb)\(mag) desde a semana passada"
    }
}

// MARK: - Headline hero

struct HeadlineCard: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles").foregroundStyle(Palette.accent)
                Text("A TUA SEMANA")
                    .font(.caption.weight(.semibold))
                    .tracking(0.6)
                    .foregroundStyle(.secondary)
            }
            Text(text)
                .font(.system(.title2, design: .rounded).weight(.semibold))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .card()
    }
}

// MARK: - Next-meal call to action

struct NextMealButton: View {
    let response: NextMealResponse?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                ZStack {
                    Circle().fill(Palette.accent.opacity(0.15)).frame(width: 46, height: 46)
                    Image(systemName: "fork.knife")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(Palette.accentText)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("O que como a seguir?")
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .card()
        }
        .buttonStyle(.plain)
    }

    private var subtitle: String {
        guard let r = response, r.isReady else { return "A preparar as ideias de hoje…" }
        let n = r.plates.count
        return "\(n) \(n == 1 ? "ideia" : "ideias") com o que já comes"
    }
}

// MARK: - Wins

struct WinsCard: View {
    let wins: [Win]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "O que está a bombar", systemImage: "checkmark.seal.fill",
                          accent: Palette.goodText)
            VStack(alignment: .leading, spacing: 12) {
                ForEach(wins) { win in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(Palette.good)
                            .font(.body)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(win.title).font(.subheadline.weight(.semibold))
                            if !win.detail.isEmpty {
                                Text(win.detail).font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .card()
        }
    }
}

// MARK: - Focus (the one thing this week)

struct FocusCard: View {
    let focus: Focus

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Coloured header band — this is the single most important element on the
            // screen, so it reads as a distinct, deliberate call rather than one card
            // among many.
            HStack(spacing: 10) {
                Image(systemName: "target")
                    .font(.headline)
                    .foregroundStyle(color)
                Text("O foco desta semana")
                    .font(.headline)
                Spacer(minLength: 0)
                Text(severityWord)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(color)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(color.opacity(0.16), in: Capsule())
            }
            .padding(.bottom, 12)

            Text(focus.label)
                .font(.system(.title3, design: .rounded).weight(.bold))
                .foregroundStyle(.primary)

            if !focus.why.isEmpty {
                Text(focus.why)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 4)
            }

            if let attribution = focus.attribution, !attribution.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "chart.pie.fill")
                        .font(.footnote)
                        .foregroundStyle(color)
                    Text(attribution)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(color.opacity(0.10),
                            in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                .padding(.top, 12)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 3)
                .fill(color)
                .frame(width: 5)
                .padding(.vertical, 18)
        }
    }

    private var color: Color {
        switch (focus.severity ?? "").lowercased() {
        case "high", "alto", "alta": return Palette.critical
        case "medium", "médio", "media", "média": return Palette.warning
        default: return Palette.accent
        }
    }

    private var severityWord: String {
        switch (focus.severity ?? "").lowercased() {
        case "high", "alto", "alta": return "PRIORIDADE"
        case "medium", "médio", "media", "média": return "A MELHORAR"
        default: return "AFINAR"
        }
    }
}

// MARK: - Swap

struct SwapCard: View {
    let swap: FoodSwap

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Troca simples", systemImage: "arrow.left.arrow.right",
                          accent: Palette.accentText)
            HStack(alignment: .center, spacing: 12) {
                swapSide(caption: "Em vez de", food: swap.from, color: .secondary,
                         strikethrough: true)
                Image(systemName: "arrow.right")
                    .font(.headline)
                    .foregroundStyle(.tertiary)
                swapSide(caption: "Experimenta", food: swap.to, color: Palette.goodText,
                         strikethrough: false)
            }
            if !swap.why.isEmpty {
                Text(swap.why)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .card()
    }

    private func swapSide(caption: String, food: String, color: Color,
                          strikethrough: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(caption.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            Text(food)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(color == .secondary ? .primary : color)
                .strikethrough(strikethrough, color: .secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Encouragement

struct EncouragementFooter: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "hand.thumbsup.fill").foregroundStyle(Palette.accent)
            Text(text)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 6)
        .padding(.top, 2)
    }
}

// MARK: - Next-meal sheet

struct NextMealSheet: View {
    let response: NextMealResponse?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if let r = response, r.isReady {
                    ScrollView {
                        VStack(spacing: 16) {
                            ForEach(r.plates.sorted { $0.rank < $1.rank }) { plate in
                                PlateCard(plate: plate)
                            }
                            if let stamp = generatedStamp(r) {
                                Text(stamp)
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                                    .frame(maxWidth: .infinity, alignment: .center)
                            }
                        }
                        .padding(16)
                    }
                    .background(Palette.screen)
                } else {
                    ContentUnavailableView {
                        Label("Ainda a preparar", systemImage: "fork.knife")
                    } description: {
                        Text("As ideias para hoje aparecem ao fim da tarde. Se estiveres "
                             + "com fome antes disso, regista o que comeres e eu ajusto.")
                    }
                }
            }
            .navigationTitle("Ideias para a refeição")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
        .presentationDetents([.large])
    }

    private func generatedStamp(_ r: NextMealResponse) -> String? {
        guard let at = r.generatedAt else { return nil }
        // "…até às 17:32" — honest about the snapshot time, since a late-logged meal
        // makes the suggestion slightly stale (an accepted trade of local generation).
        let time = String(at.suffix(8).prefix(5))
        return time.contains(":") ? "Baseado no que registaste até às \(time)" : nil
    }
}

struct PlateCard: View {
    let plate: Plate
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text(plate.title)
                    .font(.system(.title3, design: .rounded).weight(.bold))
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if plate.recommended || plate.rank == 1 {
                    Text("RECOMENDADO")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Palette.accentText)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Palette.accent.opacity(0.15), in: Capsule())
                }
            }

            // The ingredients, each with the gram range the backend computed.
            FlowLayout(spacing: 8) {
                ForEach(plate.items) { item in
                    IngredientChip(item: item)
                }
            }

            if !plate.covers.isEmpty {
                FlowLayout(spacing: 8) {
                    ForEach(plate.covers) { cover in
                        CoverPill(cover: cover)
                    }
                }
            }

            if let why = plate.why, !why.isEmpty {
                Text(why)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()

            HStack(spacing: 16) {
                if let cal = plate.calories {
                    macro(icon: "flame.fill", value: "\(Int(cal))", unit: "kcal",
                          color: Palette.fat)
                }
                if let prot = plate.proteinG {
                    macro(icon: "bolt.fill", value: "\(Int(prot))", unit: "g proteína",
                          color: Palette.protein)
                }
                Spacer(minLength: 0)
                Button {
                    UIPasteboard.general.string = plateAsText
                    withAnimation { copied = true }
                } label: {
                    Label(copied ? "Copiado" : "Copiar",
                          systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.subheadline.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .tint(copied ? Palette.good : Palette.accent)
            }
        }
        .card(padding: 18)
    }

    private func macro(icon: String, value: String, unit: String, color: Color) -> some View {
        HStack(spacing: 5) {
            Image(systemName: icon).font(.caption).foregroundStyle(color)
            Text(value).font(.subheadline.weight(.bold))
            Text(unit).font(.caption).foregroundStyle(.secondary)
        }
    }

    /// The plate as plain text, for pasting into a meal's note when logging it — the
    /// honest "tap to log" until a dedicated log-from-suggestion endpoint exists.
    private var plateAsText: String {
        let items = plate.items.map { "\($0.food) \($0.portionText)" }.joined(separator: ", ")
        return "\(plate.title): \(items)"
    }
}

/// One ingredient with its portion range; a new-to-you food carries a small badge, so
/// the novelty stands out without feeling like a chore.
struct IngredientChip: View {
    let item: PlateItem

    var body: some View {
        HStack(spacing: 6) {
            Text(item.food).font(.subheadline.weight(.medium))
            Text(item.portionText)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            if item.isNew {
                Text("novo")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(Palette.accentText)
                    .padding(.horizontal, 5).padding(.vertical, 1)
                    .background(Palette.accent.opacity(0.15), in: Capsule())
            }
        }
        .padding(.horizontal, 11).padding(.vertical, 7)
        .background(Palette.track, in: Capsule())
    }
}

struct CoverPill: View {
    let cover: Cover

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "checkmark.circle.fill")
                .font(.caption2)
                .foregroundStyle(Palette.good)
            Text(coverText)
                .font(.caption.weight(.medium))
                .foregroundStyle(Palette.goodText)
        }
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background(Palette.good.opacity(0.12), in: Capsule())
    }

    private var coverText: String {
        if let note = cover.note, !note.isEmpty { return "\(cover.label) · \(note)" }
        return cover.label
    }
}

// MARK: - Helpers

/// A short pt-PT date, e.g. "13 jul".
func shortDate(_ iso: String) -> String {
    let parser = DateFormatter()
    parser.calendar = Calendar(identifier: .gregorian)
    parser.locale = Locale(identifier: "en_US_POSIX")
    parser.dateFormat = "yyyy-MM-dd"
    guard let date = parser.date(from: iso) else { return iso }
    let out = DateFormatter()
    out.locale = Locale(identifier: "pt_PT")
    out.dateFormat = "d MMM"
    return out.string(from: date)
}

#if DEBUG
private func sampleStore() -> InsightsStore {
    let store = InsightsStore()
    store.weekly = SampleData.weeklyInsights
    store.nextMeal = SampleData.nextMeal
    return store
}

#Preview("Review") {
    InsightsView(store: sampleStore())
}

#Preview("Plates") {
    NextMealSheet(response: SampleData.nextMeal)
}
#endif
