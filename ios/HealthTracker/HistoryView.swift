//
//  HistoryView.swift
//  HealthTracker
//
//  «Histórico» — pick a past civil day and see its full macronutrient and
//  micronutrient breakdown in the same simplified layout as today. Reuses
//  the /today endpoint (which already accepts ?date=) and renders the same
//  ring-and-card layout but read-only: no meal edits, no profile, no sync
//  indicator — just what the day looked like.
//

import SwiftUI

struct HistoryView: View {
    @State private var selectedDate = Date()
    @State private var response: TodayResponse?
    @State private var isLoading = false
    @State private var errorMessage: String?

    private var isToday: Bool {
        Calendar.current.isDateInToday(selectedDate)
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                dateNavBar

                if let response {
                    ScrollView {
                        VStack(spacing: 16) {
                            Text(prettyDate(response.date))
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 4)

                            HistoryCalorieCard(response: response)
                            HistoryMacrosCard(response: response)
                            HistoryEnergyCard(response: response)
                            HistoryFlagsCard(response: response)
                            HistoryMealsCard(meals: response.meals)
                        }
                        .padding(16)
                    }
                } else if isLoading {
                    Spacer()
                    ProgressView("A carregar…")
                    Spacer()
                } else if let errorMessage {
                    Spacer()
                    ContentUnavailableView {
                        Label("Não deu para carregar", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(errorMessage)
                    } actions: {
                        Button("Tentar de novo") { load() }
                            .buttonStyle(.borderedProminent)
                    }
                    Spacer()
                } else {
                    Spacer()
                    Text("Seleciona uma data para ver o detalhe.")
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            }
            .navigationTitle("Histórico")
        }
        .onChange(of: selectedDate) { _,_ in load() }
        .task { load() }
    }

    // MARK: - Date navigation

    private var dateNavBar: some View {
        HStack(spacing: 8) {
            Button { moveDay(-1) } label: {
                Image(systemName: "chevron.left")
                    .font(.title3)
                    .fontWeight(.medium)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)

            DatePicker("Data", selection: $selectedDate,
                       in: ...Date(), displayedComponents: .date)
                .datePickerStyle(.compact)
                .labelsHidden()

            Button { moveDay(1) } label: {
                Image(systemName: "chevron.right")
                    .font(.title3)
                    .fontWeight(.medium)
            }
            .buttonStyle(.plain)
            .foregroundStyle(isToday ? Color.clear : .secondary)
            .disabled(isToday)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 16)
        .background(Palette.card)
    }

    private func moveDay(_ offset: Int) {
        guard let next = Calendar.current.date(
            byAdding: .day, value: offset, to: selectedDate
        ) else { return }
        let today = Calendar.current.startOfDay(for: Date())
        selectedDate = min(next, today)
    }

    // MARK: - Data loading

    private static func iso(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }

    private func load() {
        let date = Self.iso(selectedDate)
        Task {
            isLoading = true
            errorMessage = nil
            defer { isLoading = false }
            do {
                response = try await APIClient.shared.today(date: date)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
    }
}

// MARK: - Calorie ring

private struct HistoryCalorieCard: View {
    let response: TodayResponse
    @State private var showDetail = false

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
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .card(padding: 20)
        .contentShape(Rectangle())
        .onTapGesture { showDetail = true }
        .sheet(isPresented: $showDetail) {
            MacroDetailSheet(response: response, key: "calories",
                            title: "Calorias", unit: "kcal", color: color)
        }
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
                .foregroundStyle(.secondary).tracking(1)
            Text(big)
                .font(.system(size: 56, weight: .bold, design: .rounded))
                .monospacedDigit()
                .contentTransition(.numericText())
            Text(unit)
                .font(.subheadline).foregroundStyle(.secondary)
        }
    }
}

// MARK: - Macros row

private struct HistoryMacrosCard: View {
    let response: TodayResponse

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            historyRing("Proteína", "protein_g", Palette.protein)
            historyRing("Hidratos", "carbs_g", Palette.carbs)
            historyRing("Gordura", "fat_g", Palette.fat)
        }
        .frame(maxWidth: .infinity)
        .card()
    }

    @ViewBuilder
    private func historyRing(_ title: String, _ key: String, _ color: Color) -> some View {
        let consumed = response.consumed(key)
        let target = response.targets[key]
        let goal = target?.goal ?? 0
        let status = target.map { MetricStatus.of($0, consumed: consumed) }
        let fill = status?.fill ?? Palette.neutral
        let size: CGFloat = 90

        VStack(spacing: 8) {
            Ring(progress: goal > 0 ? consumed / goal : 0,
                 color: fill, lineWidth: size * 0.12) {
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
        .contentShape(Rectangle())
    }
}

// MARK: - Energy balance

private struct HistoryEnergyCard: View {
    let response: TodayResponse

    var body: some View {
        let intake = response.consumed("calories")
        let tdee = response.basis.tdeeKcal ?? 0
        let plan = response.basis.calorieTargetKcal ?? 0
        let scaleMax = max(intake, tdee, 1)

        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Balanço energético", systemImage: "arrow.left.arrow.right")

            bar("Ingerido", intake, Palette.accent, scaleMax)
            bar("Gasto médio", tdee, Palette.neutral, scaleMax)

            Text("Alvo de recomposição: ~\(Int(plan.rounded())) kcal. "
                 + "O gasto é a média dos últimos 14 dias.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .card()
    }

    @ViewBuilder
    private func bar(_ label: String, _ value: Double, _ color: Color, _ scaleMax: Double) -> some View {
        HStack(spacing: 12) {
            Text(label).font(.subheadline).frame(width: 100, alignment: .leading)
            TargetBar(fraction: value / scaleMax, fill: color, height: 12)
            Text("\(Int(value.rounded()))")
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 46, alignment: .trailing)
        }
    }
}

// MARK: - Limit flags

private struct HistoryFlagsCard: View {
    let response: TodayResponse

    private struct Flag: Identifiable {
        let id = UUID()
        let text: String
        let color: Color
        let symbol: String
    }

    private var flags: [Flag] {
        let keys = ["sodium_mg", "added_sugar_g", "saturated_fat_g", "trans_fat_g"]
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
                    Label { Text(flag.text).font(.subheadline) } icon: {
                        Image(systemName: flag.symbol).foregroundStyle(flag.color)
                    }
                }
            }
            .card()
        }
    }
}

// MARK: - Meal list

private struct HistoryMealsCard: View {
    let meals: [TodayMeal]
    @State private var selected: TodayMeal?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Refeições", systemImage: "fork.knife")

            if meals.isEmpty {
                Text("Nenhuma refeição registada neste dia.")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 8)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(meals.enumerated()), id: \.element.id) { index, meal in
                        Button { selected = meal } label: {
                            historyRow(meal)
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
            HistoryMealDetailSheet(meal: meal)
        }
    }

    private func historyRow(_ meal: TodayMeal) -> some View {
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

// MARK: - Read-only meal detail (no edit button)

private struct HistoryMealDetailSheet: View {
    let meal: TodayMeal
    @Environment(\.dismiss) private var dismiss
    @State private var nutrientTarget: NutrientTarget?

    private struct NutrientTarget: Identifiable {
        let id: String
        let item: MealItem
        let title: String
    }

    var body: some View {
        NavigationStack {
            List {
                let photos = meal.photoURLs
                if !photos.isEmpty {
                    Section { PhotoStrip(urls: photos) }
                }

                Section {
                    ForEach(Array(meal.items.enumerated()), id: \.offset) { _, item in
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
                        .contentShape(Rectangle())
                        .onTapGesture {
                            nutrientTarget = NutrientTarget(
                                id: item.name, item: item, title: item.name.capitalized)
                        }
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
        .sheet(item: $nutrientTarget) { target in
            ItemNutrientSheet(item: target.item, title: target.title)
        }
    }
}
