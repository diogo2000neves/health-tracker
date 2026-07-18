//
//  TodayView.swift
//  HealthTracker
//
//  «Hoje» — the one-glance answer to "how am I doing right now?". A hero calorie
//  ring, protein-first macro rings, an honest energy-balance strip, gentle flags
//  when a limit is already over, and the day's meals. Everything is measured live
//  against a personal target, never shown as a bare number.
//

import SwiftUI

struct TodayView: View {
    let store: TodayStore
    @Binding var showProfile: Bool

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
            .navigationTitle("Hoje")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showProfile = true
                    } label: {
                        Image(systemName: "person.crop.circle")
                    }
                    .accessibilityLabel("Perfil e objetivos")
                }
            }
        }
        .task { await store.load() }
    }

    @ViewBuilder
    private func content(_ r: TodayResponse) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                Text(prettyDate(r.date))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 4)

                CalorieHeroCard(response: r)
                MacrosCard(response: r)
                EnergyBalanceCard(response: r)
                FlagsCard(response: r)
                MealsCard(meals: r.meals)
            }
            .padding(16)
        }
        .refreshable { await store.load() }
    }
}

// MARK: - Calorie hero

private struct CalorieHeroCard: View {
    let response: TodayResponse

    var body: some View {
        let consumed = response.consumed("calories")
        let target = response.targets["calories"]
        let goal = target?.goal ?? 0
        let floor = target?.floor ?? 0
        let ceiling = target?.ceiling ?? goal
        let status = target.map { MetricStatus.of($0, consumed: consumed) }
        let color = status?.fill ?? Palette.accent

        VStack(spacing: 14) {
            Ring(progress: goal > 0 ? consumed / goal : 0, color: color, lineWidth: 18) {
                centre(consumed: consumed, goal: goal, floor: floor, ceiling: ceiling)
            }
            .frame(width: 208, height: 208)
            .padding(.top, 4)

            VStack(spacing: 3) {
                Text("\(Int(consumed.rounded())) de \(Int(goal.rounded())) kcal")
                    .font(.headline)
                if ceiling > floor {
                    Text("janela \(Int(floor.rounded()))–\(Int(ceiling.rounded())) kcal")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .card(padding: 20)
    }

    @ViewBuilder
    private func centre(consumed: Double, goal: Double, floor: Double, ceiling: Double) -> some View {
        let (label, big, unit): (String, String, String) = {
            if consumed > ceiling {
                return ("a mais", "\(Int((consumed - ceiling).rounded()))", "kcal")
            } else if consumed >= floor {
                return ("no alvo", "\(Int(consumed.rounded()))", "kcal")
            } else {
                return ("faltam", "\(Int((goal - consumed).rounded()))", "kcal")
            }
        }()
        VStack(spacing: 1) {
            Text(label.uppercased())
                .font(.caption).fontWeight(.semibold)
                .foregroundStyle(.secondary)
                .tracking(1)
            Text(big)
                .font(.system(size: 56, weight: .bold, design: .rounded))
                .monospacedDigit()
                .contentTransition(.numericText())
            Text(unit)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Macros (protein first & largest)

private struct MacrosCard: View {
    let response: TodayResponse

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            MacroRing(title: "Proteína", key: "protein_g", color: Palette.protein,
                      response: response)
            MacroRing(title: "Hidratos", key: "carbs_g", color: Palette.carbs,
                      response: response)
            MacroRing(title: "Gordura", key: "fat_g", color: Palette.fat,
                      response: response)
        }
        .frame(maxWidth: .infinity)
        .card()
    }
}

private struct MacroRing: View {
    let title: String
    let key: String
    let color: Color
    let response: TodayResponse
    var size: CGFloat = 90

    var body: some View {
        let consumed = response.consumed(key)
        let target = response.targets[key]
        let goal = target?.goal ?? 0
        let status = target.map { MetricStatus.of($0, consumed: consumed) }
        VStack(spacing: 8) {
            Ring(progress: goal > 0 ? consumed / goal : 0,
                 color: status?.fill ?? Palette.neutral,
                 lineWidth: size * 0.12) {
                VStack(spacing: -2) {
                    Text("\(Int(consumed.rounded()))")
                        .font(.system(size: size * 0.30, weight: .bold, design: .rounded))
                        .monospacedDigit()
                    Text("g").font(.system(size: size * 0.15)).foregroundStyle(.secondary)
                }
            }
            .frame(width: size, height: size)

            VStack(spacing: 2) {
                Text(title).font(.subheadline)
                Text("de \(Int(goal.rounded())) g")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Energy balance (honest)

private struct EnergyBalanceCard: View {
    let response: TodayResponse

    var body: some View {
        let intake = response.consumed("calories")
        let tdee = response.basis.tdeeKcal ?? 0
        let plan = response.basis.calorieTargetKcal ?? 0
        let scaleMax = max(intake, tdee, 1)

        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Balanço energético", systemImage: "arrow.left.arrow.right")

            bar(label: "Ingerido", value: intake, color: Palette.accent, scaleMax: scaleMax)
            bar(label: "Gasto médio", value: tdee, color: Palette.neutral, scaleMax: scaleMax)

            Text("Alvo de recomposição: ~\(Int(plan.rounded())) kcal (défice de ~\(Int((response.basis.calorieDeficitPct ?? 0).rounded()))%). "
                 + "O gasto é a média dos últimos 14 dias; o dia ainda vai a meio.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .card()
    }

    @ViewBuilder
    private func bar(label: String, value: Double, color: Color, scaleMax: Double) -> some View {
        HStack(spacing: 12) {
            Text(label)
                .font(.subheadline)
                .frame(width: 100, alignment: .leading)
            TargetBar(fraction: value / scaleMax, fill: color, height: 12)
            Text("\(Int(value.rounded()))")
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 46, alignment: .trailing)
        }
    }
}

// MARK: - Flags (only when a limit is already over/near)

private struct FlagsCard: View {
    let response: TodayResponse

    private struct Flag: Identifiable {
        let id = UUID()
        let text: String
        let color: Color
        let symbol: String
    }

    private var flags: [Flag] {
        let keys = ["sodium_mg", "added_sugar_g", "saturated_fat_g",
                    "trans_fat_g", "cholesterol_mg"]
        var out: [Flag] = []
        for key in keys {
            guard let target = response.targets[key],
                  target.kind == Target.Kind.limit,
                  let ceiling = target.ceiling, ceiling > 0 else { continue }
            let consumed = response.consumed(key)
            let def = NutrientCatalog.byKey[key]
            let name = def?.label ?? key
            let amount = def?.amount(consumed) ?? "\(Int(consumed))"
            let ceil = def?.amount(ceiling) ?? "\(Int(ceiling))"
            if consumed > ceiling {
                out.append(Flag(text: "\(name) acima do limite — \(amount) de \(ceil)",
                                color: Palette.critical, symbol: "exclamationmark.circle.fill"))
            } else if consumed >= 0.9 * ceiling {
                out.append(Flag(text: "\(name) perto do limite — \(amount) de \(ceil)",
                                color: Palette.warning, symbol: "exclamationmark.triangle.fill"))
            }
        }
        return out
    }

    var body: some View {
        let flags = flags
        if !flags.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(flags) { flag in
                    Label {
                        Text(flag.text).font(.subheadline)
                    } icon: {
                        Image(systemName: flag.symbol).foregroundStyle(flag.color)
                    }
                }
            }
            .card()
        }
    }
}

// MARK: - Meals

private struct MealsCard: View {
    let meals: [TodayMeal]
    @State private var selected: TodayMeal?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Refeições", systemImage: "fork.knife")

            if meals.isEmpty {
                Text("Ainda nada hoje. Regista uma refeição e ela aparece aqui.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(meals.enumerated()), id: \.element.id) { index, meal in
                        Button { selected = meal } label: {
                            MealRow(meal: meal)
                        }
                        .buttonStyle(.plain)
                        if index < meals.count - 1 {
                            Divider().padding(.leading, 58)
                        }
                    }
                }
            }
        }
        .card()
        .sheet(item: $selected) { meal in
            MealDetailSheet(meal: meal)
        }
    }
}

private struct MealRow: View {
    let meal: TodayMeal

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(meal.time)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 46, alignment: .leading)

            VStack(alignment: .leading, spacing: 3) {
                Text(meal.foods)
                    .font(.body).fontWeight(.medium)
                    .foregroundStyle(.primary)
                    .multilineTextAlignment(.leading)
                if !meal.note.isEmpty {
                    Text(meal.note).font(.caption).foregroundStyle(.secondary)
                }
                Text("P \(Int(meal.proteinG.rounded()))g · H \(Int(meal.carbsG.rounded()))g · G \(Int(meal.fatG.rounded()))g")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Spacer(minLength: 0)

            VStack(alignment: .trailing, spacing: 2) {
                Text("\(Int(meal.calories.rounded()))")
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                Text("kcal").font(.caption2).foregroundStyle(.secondary)
            }
            Image(systemName: "chevron.right")
                .font(.caption2).foregroundStyle(.tertiary)
                .padding(.top, 3)
        }
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }
}

private struct MealDetailSheet: View {
    let meal: TodayMeal
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(meal.items) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text(item.name.capitalized).fontWeight(.medium)
                                Spacer()
                                Text("\(Int(item.portionG.rounded())) g")
                                    .foregroundStyle(.secondary)
                            }
                            Text("\(Int(item.calories.rounded())) kcal · P \(Int(item.proteinG.rounded())) · H \(Int(item.carbsG.rounded())) · G \(Int(item.fatG.rounded()))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                    }
                } header: {
                    Text("\(Int(meal.calories.rounded())) kcal · \(meal.time)")
                }
                if !meal.note.isEmpty {
                    Section("Nota") { Text(meal.note) }
                }
            }
            .navigationTitle(meal.foods)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}
