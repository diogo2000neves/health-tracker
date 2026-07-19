//
//  NutrientsView.swift
//  HealthTracker
//
//  «Nutrientes» — "am I actually nourished?", answered through each nutrient's real
//  biology instead of one flat daily bar. The screen is organised around how the body
//  handles a nutrient, so the layout itself teaches the science:
//
//    • Diários   — non-cumulative (water-soluble): can't be stored, so what matters is
//                  daily CONSISTENCY. Shown as a week-dot record.
//    • Reservas  — cumulative (fat-soluble, iron, B12…): buffered by body stores, so a
//                  single low day is fine. Read against the 7-day AVERAGE.
//    • A vigiar  — a ceiling to respect: dietary limits and toxicity upper limits (ULs).
//                  Shown as floor → optimal → ceiling, so a safe surplus looks calm and a
//                  dangerous one looks dangerous.
//
//  Tap any nutrient to see which of today's foods supplied it, its kinetics, a 7-day
//  intake sparkline, and the deep reference write-up.
//

import SwiftUI

struct NutrientsView: View {
    let store: TodayStore
    @State private var info = InfoStore()
    @State private var selected: NutrientDef?

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
        .task { await info.loadIfNeeded() }
    }

    @ViewBuilder
    private func content(_ r: TodayResponse) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                NutrientHeaderCard(response: r)
                CeilingAlertCard(response: r) { selected = $0 }
                LensSection(section: .diarios, response: r) { selected = $0 }
                LensSection(section: .reservas, response: r) { selected = $0 }
                VigiarCard(response: r) { selected = $0 }
                ContextCard(response: r) { selected = $0 }
            }
            .padding(16)
        }
        .refreshable { await store.load() }
        .sheet(item: $selected) { def in
            NutrientDetailSheet(def: def, response: r, info: info)
        }
    }
}

// MARK: - Shared helpers

/// A reading for a nutrient, or nil when it has no target (a context nutrient).
private func reading(_ def: NutrientDef, _ r: TodayResponse) -> NutrientReading? {
    r.targets[def.key].map { NutrientReading(key: def.key, target: $0, response: r) }
}

private func sectionAccent(_ section: NutrientSection) -> Color {
    switch section {
    case .diarios:  return Palette.accent
    case .reservas: return Palette.muscle
    case .vigiar:   return Palette.warning
    }
}

// MARK: - Header: the three biological stories, at a glance

private struct NutrientHeaderCard: View {
    let response: TodayResponse

    var body: some View {
        HStack(spacing: 8) {
            metTile(.diarios)
            divider
            metTile(.reservas)
            divider
            watchTile
        }
        .card()
    }

    private var divider: some View {
        Rectangle().fill(Palette.track).frame(width: 1, height: 44)
    }

    private func metTile(_ section: NutrientSection) -> some View {
        let (met, total) = tally(section)
        let color: Color = (total > 0 && met == total) ? Palette.goodText
            : (total > 0 && Double(met) >= Double(total) * 0.5) ? Palette.warningText
            : .secondary
        return VStack(spacing: 3) {
            Image(systemName: section.systemImage).font(.footnote)
                .foregroundStyle(sectionAccent(section))
            Text("\(met)/\(total)")
                .font(.system(size: 22, weight: .bold, design: .rounded)).monospacedDigit()
                .foregroundStyle(color)
            Text(section.title).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private var watchTile: some View {
        let n = watchCount
        return VStack(spacing: 3) {
            Image(systemName: NutrientSection.vigiar.systemImage).font(.footnote)
                .foregroundStyle(n > 0 ? Palette.warning : Palette.good)
            Text("\(n)")
                .font(.system(size: 22, weight: .bold, design: .rounded)).monospacedDigit()
                .foregroundStyle(n > 0 ? Palette.warningText : Palette.goodText)
            Text(n == 0 ? "tudo em folga" : "a vigiar")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private func tally(_ section: NutrientSection) -> (Int, Int) {
        let defs = NutrientCatalog.members(section, targets: response.targets)
        let met = defs.reduce(0) { $0 + ((reading($1, response)?.onTarget ?? false) ? 1 : 0) }
        return (met, defs.count)
    }

    private var watchCount: Int {
        NutrientCatalog.members(.vigiar, targets: response.targets).reduce(0) { acc, def in
            guard let rd = reading(def, response) else { return acc }
            return acc + ((rd.isNearCeiling || rd.isOverCeiling) ? 1 : 0)
        }
    }
}

// MARK: - Critical banner: a breached ceiling is never buried

private struct CeilingAlertCard: View {
    let response: TodayResponse
    let onSelect: (NutrientDef) -> Void

    private var breaches: [NutrientDef] {
        NutrientCatalog.members(.vigiar, targets: response.targets)
            .filter { reading($0, response)?.isOverCeiling ?? false }
    }

    var body: some View {
        let breaches = breaches
        if !breaches.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(breaches) { def in
                    if let rd = reading(def, response) {
                        Button { onSelect(def) } label: {
                            Label {
                                Text("\(def.label) \(rd.isLimit ? "acima do limite" : "acima do teto") — \(def.amount(rd.ceilingExposure)) de \(def.amount(rd.target.ceiling ?? 0))")
                                    .font(.subheadline).foregroundStyle(.primary)
                                    .multilineTextAlignment(.leading)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            } icon: {
                                Image(systemName: "exclamationmark.octagon.fill")
                                    .foregroundStyle(Palette.critical)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .card()
            .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous)
                .strokeBorder(Palette.critical.opacity(0.45), lineWidth: 1))
        }
    }
}

// MARK: - Section title

private struct SectionTitle: View {
    let section: NutrientSection

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            SectionHeader(title: section.title, systemImage: section.systemImage,
                          accent: sectionAccent(section))
            Text(section.caption)
                .font(.caption).foregroundStyle(.secondary)
                .padding(.horizontal, 4)
        }
    }
}

// MARK: - Diários / Reservas — the floor lenses

private struct LensSection: View {
    let section: NutrientSection
    let response: TodayResponse
    let onSelect: (NutrientDef) -> Void

    var body: some View {
        let defs = NutrientCatalog.members(section, targets: response.targets)
        if !defs.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                SectionTitle(section: section)
                VStack(spacing: 18) {
                    ForEach(defs) { def in
                        NutrientRow(def: def, section: section, response: response)
                            .contentShape(Rectangle())
                            .onTapGesture { onSelect(def) }
                    }
                }
            }
            .card()
        }
    }
}

private struct NutrientRow: View {
    let def: NutrientDef
    let section: NutrientSection
    let response: TodayResponse

    var body: some View {
        if let rd = reading(def, response) {
            let floor = rd.target.floor ?? 0
            let leadsWithAverage = section == .reservas && rd.daysCounted > 0
            let lead = leadsWithAverage ? (rd.average ?? rd.today) : rd.today
            VStack(spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(def.label).font(.subheadline)
                    if leadsWithAverage {
                        Text("média").font(.caption2).foregroundStyle(.secondary)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Palette.track, in: Capsule())
                    }
                    Spacer(minLength: 8)
                    Text(def.amount(lead))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                        .foregroundStyle(rd.textColor)
                    Text(rd.label)
                        .font(.caption2.weight(.medium)).foregroundStyle(rd.fill)
                }
                lens(rd, floor)
            }
        }
    }

    @ViewBuilder
    private func lens(_ rd: NutrientReading, _ floor: Double) -> some View {
        if section == .reservas {
            if rd.daysCounted > 0 {
                RollingBar(averageFraction: floor > 0 ? (rd.average ?? 0) / floor : 0,
                           todayFraction: floor > 0 ? rd.today / floor : 0,
                           fill: rd.fill, height: 9)
                caption("hoje \(def.amount(rd.today)) · \(pct(rd.average ?? 0, floor))% do alvo")
            } else {
                TargetBar(fraction: floor > 0 ? rd.today / floor : 0, fill: rd.fill, height: 9)
                caption("de \(def.amount(floor)) · sem histórico ainda")
            }
        } else {
            if rd.daysCounted > 0 {
                HStack(spacing: 10) {
                    WeekDots(values: rd.dailyHistory, floor: floor)
                    caption("\(hitDays(rd, floor))/\(rd.daysCounted) dias no alvo")
                    Spacer(minLength: 0)
                }
            } else {
                TargetBar(fraction: floor > 0 ? rd.today / floor : 0, fill: rd.fill, height: 8)
            }
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text).font(.caption2).foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
    private func pct(_ v: Double, _ floor: Double) -> Int {
        floor > 0 ? Int((v / floor * 100).rounded()) : 0
    }
    private func hitDays(_ rd: NutrientReading, _ floor: Double) -> Int {
        rd.dailyHistory.filter { floor > 0 && $0 >= floor }.count
    }
}

// MARK: - A vigiar — limits + toxicity ceilings

private struct VigiarCard: View {
    let response: TodayResponse
    let onSelect: (NutrientDef) -> Void

    var body: some View {
        let defs = NutrientCatalog.members(.vigiar, targets: response.targets)
        let limits = defs.filter { response.targets[$0.key]?.kind == Target.Kind.limit }
        let tetos  = defs.filter { response.targets[$0.key]?.kind != Target.Kind.limit }
        if !defs.isEmpty {
            VStack(alignment: .leading, spacing: 16) {
                SectionTitle(section: .vigiar)
                if !limits.isEmpty {
                    cluster("Limites diários", limits) { LimitRow(def: $0, response: response) }
                }
                if !tetos.isEmpty {
                    cluster("Tetos de segurança", tetos) { TetoRow(def: $0, response: response) }
                }
            }
            .card()
        }
    }

    @ViewBuilder
    private func cluster<Row: View>(_ title: String, _ defs: [NutrientDef],
                                    @ViewBuilder row: @escaping (NutrientDef) -> Row) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold)).tracking(0.5)
                .foregroundStyle(.secondary).padding(.horizontal, 4)
            VStack(spacing: 16) {
                ForEach(defs) { def in
                    row(def)
                        .contentShape(Rectangle())
                        .onTapGesture { onSelect(def) }
                }
            }
        }
    }
}

/// A pure dietary limit (sodium, added sugar, sat/trans fat, cholesterol): a daily
/// budget, shown filling toward its ceiling.
private struct LimitRow: View {
    let def: NutrientDef
    let response: TodayResponse

    var body: some View {
        if let rd = reading(def, response) {
            let ceiling = rd.target.ceiling ?? 0
            VStack(spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(def.label).font(.subheadline)
                    Spacer(minLength: 8)
                    Text(def.amount(rd.today))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                        .foregroundStyle(rd.textColor)
                    Text("máx \(def.amount(ceiling))").font(.caption).foregroundStyle(.secondary)
                    Text(rd.label).font(.caption2.weight(.medium)).foregroundStyle(rd.fill)
                }
                TargetBar(fraction: ceiling > 0 ? rd.today / ceiling : 0, fill: rd.fill, height: 8)
            }
        }
    }
}

/// A nutrient with a toxicity ceiling (iron, zinc, A, selenium…): the floor → optimal
/// → UL gauge, so a healthy amount and a dangerous one never look the same.
private struct TetoRow: View {
    let def: NutrientDef
    let response: TodayResponse

    var body: some View {
        if let rd = reading(def, response) {
            let ceiling = rd.target.ceiling ?? 0
            let rolling = rd.target.isRolling && rd.daysCounted > 0
            VStack(spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(def.label).font(.subheadline)
                    Spacer(minLength: 8)
                    Text(def.amount(rd.ceilingExposure))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                        .foregroundStyle(rd.textColor)
                    Text("teto \(def.amount(ceiling))").font(.caption).foregroundStyle(.secondary)
                    Text(rd.label).font(.caption2.weight(.medium)).foregroundStyle(rd.fill)
                }
                RangeGauge(floor: rd.floor, ceiling: ceiling,
                           current: rd.ceilingExposure, marker: rd.fill, height: 11)
                if rolling, let avg = rd.average {
                    Text("hoje \(def.amount(rd.today)) · média 7 d \(def.amount(avg))")
                        .font(.caption2).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}

// MARK: - Context — measured, but no target (the ratio/quality fats, total sugar)

private struct ContextCard: View {
    let response: TodayResponse
    let onSelect: (NutrientDef) -> Void

    var body: some View {
        let defs = NutrientCatalog.context(consumed: response.consumed, targets: response.targets)
        if !defs.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(title: "Contexto", systemImage: "circle.dashed", accent: .secondary)
                VStack(spacing: 12) {
                    ForEach(Array(defs.enumerated()), id: \.element.id) { index, def in
                        HStack {
                            Text(def.label).font(.subheadline)
                            Spacer(minLength: 8)
                            Text(def.amount(response.consumed(def.key)))
                                .font(.subheadline.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { onSelect(def) }
                        if index < defs.count - 1 { Divider() }
                    }
                }
            }
            .card()
        }
    }
}

// MARK: - Drill-down: kinetics, sparkline, which foods supplied this nutrient

private struct NutrientDetailSheet: View {
    let def: NutrientDef
    let response: TodayResponse
    let info: InfoStore
    @Environment(\.dismiss) private var dismiss

    private struct Contributor: Identifiable {
        let id = UUID()
        let name: String
        let time: String
        let amount: Double
    }

    var body: some View {
        let contributors = contributors()
        let maxAmount = contributors.map(\.amount).max() ?? 0
        let rd = reading(def, response)
        let write = info.info(for: def.key)

        NavigationStack {
            List {
                Section {
                    header(rd)
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 12, trailing: 16))
                }
                if let rd, !rd.dailyHistory.isEmpty {
                    Section {
                        IntakeBars(values: rd.dailyHistory, today: rd.today,
                                   floor: rd.floor, ceiling: rd.target.ceiling)
                            .frame(height: 64)
                            .listRowInsets(EdgeInsets(top: 12, leading: 16, bottom: 8, trailing: 16))
                    } header: {
                        Text("Últimos dias")
                    } footer: {
                        Text("Cada barra é um dia; a mais forte é hoje. A linha é o alvo.")
                    }
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
                if let write {
                    Section {
                        NavigationLink {
                            NutrientInfoView(def: def, response: response, info: write)
                        } label: {
                            Label("Saber mais sobre \(def.label)", systemImage: "book.pages")
                                .font(.subheadline.weight(.medium))
                        }
                    } footer: {
                        if let summary = write.summary, !summary.isEmpty {
                            Text(summary)
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
    private func header(_ rd: NutrientReading?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let rd {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(def.amount(rd.today))
                        .font(.system(size: 30, weight: .bold, design: .rounded)).monospacedDigit()
                    Text(descriptor(rd)).font(.subheadline).foregroundStyle(.secondary)
                    Spacer()
                    Label(rd.label, systemImage: rd.symbol.isEmpty ? "circle" : rd.symbol)
                        .font(.caption.weight(.semibold)).foregroundStyle(rd.fill)
                        .labelStyle(.titleAndIcon)
                }
                primaryBar(rd)
                Text(kineticsBlurb(rd)).font(.caption).foregroundStyle(.secondary)
            } else {
                Text(def.amount(response.consumed(def.key)))
                    .font(.system(size: 30, weight: .bold, design: .rounded)).monospacedDigit()
                Text("Sem alvo definido — mostrado apenas como contexto.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func primaryBar(_ rd: NutrientReading) -> some View {
        let floor = rd.target.floor ?? 0
        if let ul = rd.upperLimit {
            RangeGauge(floor: rd.floor, ceiling: ul, current: rd.ceilingExposure,
                       marker: rd.fill, height: 12)
        } else if rd.isLimit, let c = rd.target.ceiling {
            TargetBar(fraction: c > 0 ? rd.today / c : 0, fill: rd.fill, height: 10)
        } else if rd.target.isRolling, rd.daysCounted > 0 {
            RollingBar(averageFraction: floor > 0 ? (rd.average ?? 0) / floor : 0,
                       todayFraction: floor > 0 ? rd.today / floor : 0, fill: rd.fill, height: 10)
        } else {
            TargetBar(fraction: floor > 0 ? rd.today / floor : 0, fill: rd.fill, height: 10)
        }
    }

    private func descriptor(_ rd: NutrientReading) -> String {
        if rd.isLimit { return "máx \(def.amount(rd.target.ceiling ?? 0))" }
        if let ul = rd.upperLimit {
            let floorTxt = rd.target.floor.map { "de \(def.amount($0)) " } ?? ""
            return "\(floorTxt)· teto \(def.amount(ul))"
        }
        if rd.target.isRolling, rd.daysCounted > 0, let avg = rd.average {
            return "média 7 d \(def.amount(avg)) · de \(def.amount(rd.target.floor ?? 0))"
        }
        return "de \(def.amount(rd.target.floor ?? rd.target.goal))"
    }

    /// The one-line explanation of WHY this nutrient reads the way it does — the science
    /// that turns "you're at 20% today" into a calm, correct message.
    private func kineticsBlurb(_ rd: NutrientReading) -> String {
        if rd.isLimit {
            return "Limite diário — o objetivo é ficar abaixo do máximo, todos os dias."
        }
        if rd.target.isRolling {
            var s = "O corpo acumula reservas deste nutriente, por isso um dia mais baixo não é alarme — o que conta é a média de vários dias."
            if rd.upperLimit != nil {
                s += " Tem também um teto de segurança que não deves ultrapassar."
            }
            return s
        }
        var s = "Não se armazena em quantidade útil — o excedente é eliminado. Conta a consistência diária, não um dia com muito."
        if rd.upperLimit != nil { s += " Ainda assim, tem um teto de segurança." }
        return s
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

/// A compact bar-per-day sparkline of the rolling window plus today, with a reference
/// line at the floor. Bars that met the floor are green, misses are muted, and a day
/// over the ceiling is red — so a nutrient's pattern reads at a glance.
private struct IntakeBars: View {
    let values: [Double]        // completed days, oldest first
    let today: Double
    let floor: Double?
    let ceiling: Double?

    var body: some View {
        let all = values + [today]
        let scale = max(all.max() ?? 1, floor ?? 0, 1)
        GeometryReader { geo in
            let h = geo.size.height
            ZStack(alignment: .bottomLeading) {
                if let f = floor, f > 0 {
                    let y = CGFloat(min(f / scale, 1)) * h
                    Rectangle().fill(Palette.neutral.opacity(0.5))
                        .frame(height: 1)
                        .frame(maxHeight: .infinity, alignment: .bottom)
                        .offset(y: -y)
                }
                HStack(alignment: .bottom, spacing: 6) {
                    ForEach(Array(all.enumerated()), id: \.offset) { index, v in
                        let isToday = index == all.count - 1
                        let met = (floor ?? 0) > 0 && v >= (floor ?? 0)
                        let over = ceiling.map { v > $0 } ?? false
                        let color = over ? Palette.critical : (met ? Palette.good : Palette.neutral)
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(color.opacity(isToday ? 1 : 0.5))
                            .frame(height: max(3, CGFloat(min(v / scale, 1)) * h))
                            .frame(maxWidth: .infinity)
                    }
                }
            }
        }
    }
}

// MARK: - Deep info: the per-nutrient reference screen

/// The clean, sectioned write-up for one nutrient. Renders only the sections that
/// have content, so a sparsely-filled entry still looks intentional. Pushed from
/// the tap-popup's "Saber mais" link.
private struct NutrientInfoView: View {
    let def: NutrientDef
    let response: TodayResponse
    let info: NutrientInfo

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if let summary = info.summary, !summary.isEmpty {
                    Text(summary)
                        .font(.body)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .card()
                }

                todayRecap
                recommendationCard

                bulletsCard("Funções", "bolt.heart.fill", Palette.accent, info.roles)

                textCard("Para o teu objetivo", "figure.strengthtraining.traditional",
                         Palette.muscle, info.goalRelevance)

                if let foods = info.foodSources, !foods.isEmpty {
                    InfoCard(title: "Fontes alimentares", systemImage: "carrot.fill",
                             accent: Palette.good) {
                        VStack(spacing: 10) {
                            ForEach(Array(foods.enumerated()), id: \.element.id) { index, food in
                                VStack(spacing: 6) {
                                    HStack(alignment: .firstTextBaseline) {
                                        Text(food.food).font(.subheadline)
                                        Spacer(minLength: 8)
                                        Text(food.amountText)
                                            .font(.subheadline.weight(.semibold).monospacedDigit())
                                            .foregroundStyle(Palette.goodText)
                                    }
                                    if let note = food.note, !note.isEmpty {
                                        Text(note)
                                            .font(.caption).foregroundStyle(.secondary)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }
                                if index < foods.count - 1 { Divider() }
                            }
                        }
                    }
                }

                textCard("Se faltar", "arrow.down.circle.fill", Palette.warning, info.deficiency)

                if hasExcess {
                    InfoCard(title: "Se em excesso", systemImage: "exclamationmark.triangle.fill",
                             accent: Palette.critical) {
                        VStack(alignment: .leading, spacing: 8) {
                            if let excess = info.excess, !excess.isEmpty {
                                Text(excess).font(.body)
                            }
                            if let ul = info.upperLimit, !ul.isEmpty {
                                Label(ul, systemImage: "gauge.with.dots.needle.67percent")
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                    }
                }

                bulletsCard("Dicas", "lightbulb.fill", Palette.accent, info.tips)

                if let fact = info.fact, !fact.isEmpty {
                    Label {
                        Text(fact).font(.subheadline)
                    } icon: {
                        Image(systemName: "sparkles").foregroundStyle(Palette.warning)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
                    .background(Palette.warning.opacity(0.12),
                                in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                }

                if let sections = info.sections {
                    ForEach(sections) { section in
                        InfoCard(title: section.title, systemImage: "text.alignleft",
                                 accent: .secondary) {
                            Text(section.body).font(.body)
                        }
                    }
                }

                if let refs = info.references, !refs.isEmpty {
                    Text("Fontes: " + refs.joined(separator: " · "))
                        .font(.caption2).foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 4)
                }
            }
            .padding(16)
        }
        .background(Palette.screen)
        .navigationTitle(def.label)
        .navigationBarTitleDisplayMode(.inline)
    }

    private var hasExcess: Bool {
        !(info.excess ?? "").isEmpty || !(info.upperLimit ?? "").isEmpty
    }

    /// A slim "your intake today" strip, so the education is grounded in real data.
    @ViewBuilder
    private var todayRecap: some View {
        let consumed = response.consumed(def.key)
        if let target = response.targets[def.key] {
            let status = MetricStatus.of(target, consumed: consumed)
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Hoje").font(.subheadline).foregroundStyle(.secondary)
                    Spacer()
                    Text(def.amount(consumed))
                        .font(.subheadline.weight(.semibold).monospacedDigit())
                        .foregroundStyle(status.text)
                    Text(target.kind == Target.Kind.limit
                         ? "máx \(def.amount(target.ceiling ?? 0))"
                         : "de \(def.amount(target.floor ?? target.goal))")
                        .font(.caption).foregroundStyle(.secondary)
                }
                TargetBar(fraction: status.fraction, fill: status.fill, height: 8)
            }
            .card()
        }
    }

    /// The daily reference values: RDA (the target floor), the optimal range, and
    /// the upper limit — the numbers to keep front-and-centre.
    @ViewBuilder
    private var recommendationCard: some View {
        let target = response.targets[def.key]
        let rda: String? = {
            guard let target, target.kind != Target.Kind.limit, let floor = target.floor
            else { return nil }
            return def.amount(floor)
        }()
        let hasAny = rda != nil
            || !(info.optimalRange ?? "").isEmpty
            || !(info.upperLimit ?? "").isEmpty
        if hasAny {
            InfoCard(title: "Recomendação diária", systemImage: "ruler.fill",
                     accent: Palette.accent) {
                VStack(spacing: 10) {
                    if let rda { valueRow("Recomendado", rda) }
                    if let optimal = info.optimalRange, !optimal.isEmpty {
                        valueRow("Ótimo", optimal, highlight: true)
                    }
                    if let ul = info.upperLimit, !ul.isEmpty {
                        valueRow("Limite máximo", ul)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func valueRow(_ label: String, _ value: String, highlight: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.subheadline).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.subheadline.weight(highlight ? .semibold : .regular).monospacedDigit())
                .foregroundStyle(highlight ? Palette.accentText : .primary)
                .multilineTextAlignment(.trailing)
        }
    }

    @ViewBuilder
    private func textCard(_ title: String, _ symbol: String, _ accent: Color,
                          _ text: String?) -> some View {
        if let text, !text.isEmpty {
            InfoCard(title: title, systemImage: symbol, accent: accent) {
                Text(text).font(.body)
            }
        }
    }

    @ViewBuilder
    private func bulletsCard(_ title: String, _ symbol: String, _ accent: Color,
                             _ items: [String]?) -> some View {
        if let items, !items.isEmpty {
            InfoCard(title: title, systemImage: symbol, accent: accent) {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(items, id: \.self) { item in
                        HStack(alignment: .top, spacing: 10) {
                            Circle().fill(accent).frame(width: 6, height: 6).padding(.top, 7)
                            Text(item).font(.body)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
        }
    }
}

/// A titled card used throughout the nutrient detail screen.
private struct InfoCard<Content: View>: View {
    let title: String
    let systemImage: String
    var accent: Color = .secondary
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: title, systemImage: systemImage, accent: accent)
            content
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .card()
    }
}
