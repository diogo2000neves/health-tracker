//
//  NutrientsView.swift
//  HealthTracker
//
//  «Nutrientes» — "am I actually nourished?". Every micronutrient shown as a bar
//  against its reference, grouped (limits to watch first, then vitamins, minerals,
//  fats & fibre), coloured by whether it's a floor to reach or a ceiling to stay
//  under. Tap any nutrient to see exactly which of today's foods supplied it — the
//  app's signature feature, possible because every meal carries per-ingredient
//  nutrients.
//

import SwiftUI

struct NutrientsView: View {
    let store: TodayStore

    var body: some View {
        NavigationStack {
            ZStack {
                Palette.screen.ignoresSafeArea()
                if let response = store.response {
                    content(response)
                } else {
                    LoadingOrError(isLoading: store.isLoading,
                                   error: store.errorMessage) {
                        Task { await store.load() }
                    }
                }
            }
            .navigationTitle("Nutrientes")
        }
        .task { await store.load() }
    }

    @ViewBuilder
    private func content(_ r: TodayResponse) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                NutrientSummaryCard(response: r)
                ForEach(NutrientGroup.allCases) { group in
                    NutrientGroupCard(group: group, response: r)
                }
            }
            .padding(16)
        }
        .refreshable { await store.load() }
    }
}

// MARK: - Summary

private struct NutrientSummaryCard: View {
    let response: TodayResponse

    var body: some View {
        let (met, total) = tally
        let fraction = total > 0 ? Double(met) / Double(total) : 0
        let summaryColor = fraction >= 0.7 ? Palette.good
            : fraction >= 0.4 ? Palette.warning
            : Palette.accent
        let summaryText = fraction >= 0.7 ? Palette.goodText
            : fraction >= 0.4 ? Palette.warningText
            : Palette.accentText

        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("\(met)")
                    .font(.system(size: 40, weight: .bold, design: .rounded))
                    .foregroundStyle(summaryText)
                Text("de \(total) no alvo")
                    .font(.headline)
                    .foregroundStyle(.secondary)
            }
            TargetBar(fraction: fraction, fill: summaryColor, height: 10)
            Text(summarySubtitle(met: met, total: total))
                .font(.caption).foregroundStyle(.secondary)
        }
        .card()
    }

    private func summarySubtitle(met: Int, total: Int) -> String {
        let missing = total - met
        if missing == 0 { return "Todos os nutrientes dentro do alvo — excelente!" }
        if missing == 1 { return "1 nutriente ainda fora do alvo." }
        if missing <= 5 { return "\(missing) nutrientes ainda fora do alvo." }
        return "\(missing) nutrientes ainda fora do alvo. O dia ainda não acabou."
    }

    /// Count metrics that are on target across every group that has a target: a
    /// reach floor met, or a limit ceiling respected.
    private var tally: (Int, Int) {
        var met = 0, total = 0
        for group in NutrientGroup.allCases {
            for def in NutrientCatalog.defs(group) {
                guard let target = response.targets[def.key] else { continue }
                total += 1
                if MetricStatus.of(target, consumed: response.consumed(def.key)).onTarget {
                    met += 1
                }
            }
        }
        return (met, total)
    }
}

// MARK: - A group of nutrients

private struct NutrientGroupCard: View {
    let group: NutrientGroup
    let response: TodayResponse
    @State private var selected: NutrientDef?

    var body: some View {
        let defs = NutrientCatalog.defs(group)
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: group.title, systemImage: group.systemImage)
            VStack(spacing: 16) {
                ForEach(defs) { def in
                    NutrientRow(def: def, response: response)
                        .contentShape(Rectangle())
                        .onTapGesture { selected = def }
                }
            }
        }
        .card()
        .sheet(item: $selected) { def in
            NutrientDetailSheet(def: def, response: response)
        }
    }
}

private struct NutrientRow: View {
    let def: NutrientDef
    let response: TodayResponse

    var body: some View {
        let consumed = response.consumed(def.key)
        let target = response.targets[def.key]

        VStack(spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(def.label).font(.subheadline)
                Spacer(minLength: 8)
                if let target {
                    let status = MetricStatus.of(target, consumed: consumed)
                    Text(def.amount(consumed))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                        .foregroundStyle(status.text)
                    Text(targetSuffix(target))
                        .font(.caption).foregroundStyle(.secondary)
                    Text(statusLabel(target, consumed: consumed))
                        .font(.caption2).fontWeight(.medium)
                        .foregroundStyle(status.fill)
                } else {
                    // context nutrient — no target, show the amount only
                    Text(def.amount(consumed))
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            if let target {
                let status = MetricStatus.of(target, consumed: consumed)
                TargetBar(fraction: status.fraction, fill: status.fill, height: 8)
            }
        }
    }

    /// "de 90 mg" for a reach floor, "máx 2300 mg" for a limit, "233–285" for a window.
    private func targetSuffix(_ target: Target) -> String {
        switch target.kind {
        case Target.Kind.limit:
            return "máx \(def.amount(target.ceiling ?? 0))"
        case Target.Kind.window:
            let lo = Int((target.floor ?? 0).rounded())
            let hi = Int((target.ceiling ?? 0).rounded())
            return "\(lo)–\(hi) \(def.unit)"
        default:
            return "de \(def.amount(target.floor ?? 0))"
        }
    }

    /// Clear Portuguese status labels, replacing ambiguous icons.
    private func statusLabel(_ target: Target, consumed: Double) -> String {
        switch target.kind {
        case Target.Kind.limit:
            let ceiling = target.ceiling ?? target.goal
            if consumed > ceiling { return "acima" }
            let frac = ceiling > 0 ? consumed / ceiling : 0
            if frac >= 0.8 { return "perto do limite" }
            return "OK"
        case Target.Kind.window:
            let floor = target.floor ?? 0
            let ceiling = target.ceiling ?? target.goal
            if consumed > ceiling { return "em excesso" }
            if consumed >= floor { return "no alvo" }
            return "a caminho"
        default: // reach
            let floor = target.floor ?? target.goal
            if consumed >= floor { return "atingido" }
            let frac = floor > 0 ? consumed / floor : 0
            if frac >= 0.6 { return "quase lá" }
            return "em falta"
        }
    }
}

// MARK: - Drill-down: which foods supplied this nutrient

private struct NutrientDetailSheet: View {
    let def: NutrientDef
    let response: TodayResponse
    @Environment(\.dismiss) private var dismiss

    private struct Contributor: Identifiable {
        let id = UUID()
        let name: String
        let time: String
        let amount: Double
    }

    var body: some View {
        let contributors = contributors()
        let total = contributors.reduce(0) { $0 + $1.amount }
        let maxAmount = contributors.map(\.amount).max() ?? 0
        let target = response.targets[def.key]

        NavigationStack {
            List {
                Section {
                    header(total: total, target: target)
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 12, trailing: 16))
                }
                if contributors.isEmpty {
                    Section {
                        Text("Nenhuma refeição de hoje contribuiu com \(def.label.lowercased()).")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Section("Origem hoje") {
                        ForEach(contributors) { c in
                            VStack(spacing: 6) {
                                HStack {
                                    Text(c.name.capitalized).font(.subheadline)
                                    Text(c.time).font(.caption).foregroundStyle(.tertiary)
                                    Spacer()
                                    Text(def.amount(c.amount))
                                        .font(.subheadline.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                }
                                TargetBar(fraction: maxAmount > 0 ? c.amount / maxAmount : 0,
                                          fill: Palette.accent, height: 6)
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            }
            .navigationTitle(def.label)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    @ViewBuilder
    private func header(total: Double, target: Target?) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(def.amount(total))
                    .font(.system(size: 30, weight: .bold, design: .rounded))
                    .monospacedDigit()
                if let target {
                    let status = MetricStatus.of(target, consumed: total)
                    Text(descriptor(target))
                        .font(.subheadline).foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: status.symbol.isEmpty ? "circle" : status.symbol)
                        .foregroundStyle(status.fill)
                }
            }
            if let target {
                let status = MetricStatus.of(target, consumed: total)
                TargetBar(fraction: status.fraction, fill: status.fill, height: 10)
            }
        }
    }

    private func descriptor(_ target: Target) -> String {
        switch target.kind {
        case Target.Kind.limit:  return "limite \(def.amount(target.ceiling ?? 0))"
        case Target.Kind.window: return "alvo \(Int((target.floor ?? 0).rounded()))–\(Int((target.ceiling ?? 0).rounded())) \(def.unit)"
        default:                 return "alvo \(def.amount(target.floor ?? 0))"
        }
    }

    /// Every food that contributed this nutrient today, biggest first.
    private func contributors() -> [Contributor] {
        var out: [Contributor] = []
        for meal in response.meals {
            for item in meal.items {
                let amount = item.nutrients[def.key] ?? 0
                if amount > 0 {
                    out.append(Contributor(name: item.name, time: meal.time, amount: amount))
                }
            }
        }
        return out.sorted { $0.amount > $1.amount }
    }
}
