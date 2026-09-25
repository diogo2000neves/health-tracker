//
//  AddMealView.swift
//  HealthTracker
//
//  «Adicionar refeição»: log a meal by starting from one already eaten.
//
//  The question "which day did I eat that?" never has to be answered. The top of
//  the list is the user's habits — found by the server in the history, and ordered
//  for the time of day (the usual breakfast in the morning, the usual lunch at
//  lunchtime) — then the recent meals day by day, and a search over all of it.
//  Picking one opens the editor pre-filled with its ingredients; adjust the grams,
//  add the banana, save. Nothing goes through the model unless a new food is
//  described in words.
//

import SwiftUI

struct AddMealView: View {
    /// The day the meal is logged on ("yyyy-MM-dd") — today, or the past day the
    /// user was looking at.
    let day: String
    /// Called once a meal was saved; this sheet has already closed itself.
    let onFinished: () -> Void

    @Environment(\.dismiss) private var dismiss
    private let store = MealLibraryStore.shared
    @State private var query = ""

    private var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Adicionar refeição")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancelar") { dismiss() }
                    }
                }
                .navigationDestination(for: MealEditorMode.self) { mode in
                    MealEditorView(mode: mode, isModal: false) {
                        dismiss()
                        onFinished()
                    }
                }
        }
        .task { await store.load() }
    }

    @ViewBuilder
    private var content: some View {
        if let library = store.library {
            List {
                if trimmed.isEmpty {
                    browse(library)
                } else {
                    results(library)
                }
            }
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                        prompt: "Procurar no histórico")
        } else if let error = store.errorMessage {
            LoadingOrError(isLoading: false, error: error) {
                Task { await store.load() }
            }
        } else {
            ProgressView("A carregar o histórico…")
        }
    }

    // MARK: - Browsing

    @ViewBuilder
    private func browse(_ library: MealLibrary) -> some View {
        if !library.suggestions.isEmpty {
            Section {
                ForEach(library.suggestions) { habit in
                    NavigationLink(value: MealEditorMode.create(
                        day: day, base: habit.meal, versions: habit.versions)) {
                        HabitRow(habit: habit)
                    }
                }
            } header: {
                Text("Sugestões para agora")
            } footer: {
                Text("O que costumas repetir, primeiro o que é habitual a esta hora. Abre, ajusta as quantidades e regista.")
            }
        }

        Section {
            NavigationLink(value: MealEditorMode.create(day: day, base: nil, versions: [])) {
                Label("Começar do zero", systemImage: "square.and.pencil")
            }
        } footer: {
            Text("Junta ingredientes do teu histórico, ou descreve um novo e a IA estima-o.")
        }

        ForEach(Self.byDay(library.recent)) { group in
            Section(MealText.dayLabel(group.day)) {
                ForEach(group.meals) { meal in
                    NavigationLink(value: MealEditorMode.create(
                        day: day, base: meal, versions: [])) {
                        LibraryMealRow(meal: meal, showsDay: false)
                    }
                }
            }
        }
    }

    // MARK: - Searching

    @ViewBuilder
    private func results(_ library: MealLibrary) -> some View {
        let habits = library.suggestions.filter { matches($0.meal) }
        let meals = library.allMeals.filter(matches)
        if habits.isEmpty && meals.isEmpty {
            Section {
                NavigationLink(value: MealEditorMode.create(day: day, base: nil, versions: [])) {
                    Label("Começar do zero", systemImage: "square.and.pencil")
                }
            } header: {
                Text("Nada no histórico com «\(trimmed)»")
            } footer: {
                Text("Começa do zero e descreve-o — a IA estima os valores.")
            }
        }
        if !habits.isEmpty {
            Section("Hábitos") {
                ForEach(habits) { habit in
                    NavigationLink(value: MealEditorMode.create(
                        day: day, base: habit.meal, versions: habit.versions)) {
                        HabitRow(habit: habit)
                    }
                }
            }
        }
        if !meals.isEmpty {
            Section("Refeições") {
                ForEach(meals) { meal in
                    NavigationLink(value: MealEditorMode.create(
                        day: day, base: meal, versions: [])) {
                        LibraryMealRow(meal: meal, showsDay: true)
                    }
                }
            }
        }
    }

    private func matches(_ meal: TodayMeal) -> Bool {
        let text = ([meal.foods, meal.note] + meal.items.map { "\($0.name) \($0.key)" })
            .joined(separator: " ")
        return MealText.matches(text, query: trimmed)
    }

    // MARK: - Grouping

    private struct DayGroup: Identifiable {
        let day: String
        let meals: [TodayMeal]
        var id: String { day }
    }

    /// Recent meals, one section per day, newest day first and each day in the
    /// order it was eaten.
    private static func byDay(_ meals: [TodayMeal]) -> [DayGroup] {
        Dictionary(grouping: meals, by: \.day)
            .map { entry in
                DayGroup(day: entry.key,
                         meals: entry.value.sorted { $0.datetime < $1.datetime })
            }
            .sorted { $0.day > $1.day }
    }
}

// MARK: - Rows

/// A habit: what it is, how often, when, and what it comes to.
private struct HabitRow: View {
    let habit: MealHabit

    var body: some View {
        let meal = habit.meal
        VStack(alignment: .leading, spacing: 6) {
            Text(MealText.sentence(meal.foods))
                .font(.body.weight(.medium))
                .lineLimit(2)
            HStack(spacing: 12) {
                Label("\(habit.count)×", systemImage: "arrow.triangle.2.circlepath")
                Label("~\(habit.typicalTime)", systemImage: "clock")
                Spacer(minLength: 0)
                Text("\(Int(meal.calories.rounded())) kcal")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
            }
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(.secondary)
            MacroLine(meal: meal)
        }
        .padding(.vertical, 4)
    }
}

/// One past meal: its time, foods and calories.
private struct LibraryMealRow: View {
    let meal: TodayMeal
    let showsDay: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(meal.time)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: 46, alignment: .leading)
            VStack(alignment: .leading, spacing: 3) {
                Text(MealText.sentence(meal.foods))
                    .fontWeight(.medium)
                    .lineLimit(2)
                if showsDay {
                    Text(MealText.dayLabel(meal.datetime))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                MacroLine(meal: meal)
            }
            Spacer(minLength: 0)
            VStack(alignment: .trailing, spacing: 2) {
                Text("\(Int(meal.calories.rounded()))")
                    .font(.subheadline.weight(.semibold).monospacedDigit())
                Text("kcal")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

/// "P 32 · H 58 · G 14", each letter in its macro's colour.
private struct MacroLine: View {
    let meal: TodayMeal

    var body: some View {
        HStack(spacing: 8) {
            part("P", meal.proteinG, Palette.protein)
            part("H", meal.carbsG, Palette.carbs)
            part("G", meal.fatG, Palette.fat)
        }
        .font(.caption)
        .monospacedDigit()
    }

    private func part(_ letter: String, _ grams: Double, _ color: Color) -> some View {
        HStack(spacing: 2) {
            Text(letter).fontWeight(.semibold).foregroundStyle(color)
            Text("\(Int(grams.rounded()))g").foregroundStyle(.secondary)
        }
    }
}
