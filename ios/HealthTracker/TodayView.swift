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

    // Historical date browsing state
    @State private var showCalendar = false
    @State private var pickerDate = Date()
    @State private var historicalDate: Date?
    @State private var historicalResponse: TodayResponse?
    @State private var isHistoricalLoading = false
    /// Why a past day could not be shown. Said out loud: falling back to today in
    /// silence once hid a decoding break for hours behind a stale cached screen.
    @State private var historicalError: String?

    private var isHistorical: Bool { historicalResponse != nil }
    private var activeResponse: TodayResponse? { historicalResponse ?? store.response }

    var body: some View {
        NavigationStack {
            ZStack {
                Palette.screen.ignoresSafeArea()
                if isHistoricalLoading {
                    VStack(spacing: 12) {
                        Spacer()
                        ProgressView().controlSize(.large)
                        Text("A carregar…")
                            .font(.subheadline).foregroundStyle(.secondary)
                        Spacer()
                    }
                } else if let response = activeResponse {
                    content(response)
                } else {
                    LoadingOrError(isLoading: store.isLoading,
                                   error: store.errorMessage) {
                        Task { await store.load() }
                    }
                }
            }
            .navigationTitle(isHistorical ? prettyDate(historicalResponse!.date) : "Hoje")
            .toolbar {
                if store.isRefreshing && !isHistorical {
                    ToolbarItem(placement: .topBarLeading) {
                        SyncIndicator()
                    }
                }
                if isHistorical {
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            withAnimation { backToToday() }
                        } label: {
                            HStack(spacing: 4) {
                                Image(systemName: "arrow.left")
                                Text("Hoje").fontWeight(.medium)
                            }
                        }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showCalendar = true } label: {
                        Image(systemName: "calendar")
                    }
                }
                if !isHistorical {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { showProfile = true } label: {
                            Image(systemName: "person.crop.circle")
                        }
                        .accessibilityLabel("Perfil e objetivos")
                    }
                }
            }
        }
        .task { await store.load() }
        // An ingredient described in words is estimated in the background; keep
        // re-reading the day until it lands, so it appears without a pull.
        .task(id: pendingKey) {
            guard pendingKey != nil else { return }
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(20))
                guard !Task.isCancelled else { return }
                await reload()
            }
        }
        .alert("Não deu para abrir esse dia",
               isPresented: Binding(get: { historicalError != nil },
                                    set: { if !$0 { historicalError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(historicalError ?? "")
        }
        .sheet(isPresented: $showCalendar) {
            NavigationStack {
                DatePicker("Data", selection: $pickerDate,
                           in: ...Date(), displayedComponents: .date)
                    .datePickerStyle(.graphical)
                    .padding()
                    .navigationTitle("Ver dia")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) {
                            Button("Cancelar") { showCalendar = false }
                        }
                        ToolbarItem(placement: .confirmationAction) {
                            Button("OK") {
                                showCalendar = false
                                historicalDate = pickerDate
                                loadHistorical(pickerDate)
                            }
                        }
                    }
            }
            .presentationDetents([.medium])
        }
    }

    /// The meals on screen still waiting for an estimate, or nil when none are.
    private var pendingKey: String? {
        let ids = (activeResponse?.meals ?? []).filter(\.hasPendingItems).map(\.datetime)
        return ids.isEmpty ? nil : ids.joined(separator: "|")
    }

    /// Re-read what is on screen: today, and the past day being viewed if any.
    private func reload() async {
        await store.load()
        if let date = historicalDate,
           let fresh = try? await APIClient.shared.today(date: Self.iso(date)) {
            historicalResponse = fresh
        }
    }

    /// After a meal was saved or deleted in the app: the day changed, so the
    /// coach's cards are stale too.
    private func mealsChanged() async {
        store.noteEdit()
        await reload()
    }

    private func backToToday() {
        historicalResponse = nil
        historicalDate = nil
        isHistoricalLoading = false
    }

    private func loadHistorical(_ date: Date) {
        isHistoricalLoading = true
        historicalResponse = nil
        let iso = Self.iso(date)
        Task {
            do {
                historicalResponse = try await APIClient.shared.today(date: iso)
            } catch {
                backToToday()
                historicalError = error.localizedDescription
            }
            isHistoricalLoading = false
        }
    }

    private static func iso(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
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
                FlagsCard(response: r)
                MealsCard(meals: r.meals, day: r.date) { await mealsChanged() }
            }
            .padding(16)
        }
        .refreshable { await reload() }
    }
}

// MARK: - Calorie hero

private struct CalorieHeroCard: View {
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
                        .font(.caption)
                        .foregroundStyle(.secondary)
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
    @State private var showDetail = false

    var body: some View {
        let consumed = response.consumed(key)
        let target = response.targets[key]
        let goal = target?.goal ?? 0
        let status = target.map { MetricStatus.of($0, consumed: consumed) }
        let fill = status?.fill ?? Palette.neutral
        VStack(spacing: 8) {
            Ring(progress: goal > 0 ? consumed / goal : 0,
                 color: fill,
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
        .contentShape(Rectangle())
        .onTapGesture { showDetail = true }
        .sheet(isPresented: $showDetail) {
            MacroDetailSheet(response: response, key: key,
                            title: title, unit: "g", color: fill)
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
    /// The day on screen ("yyyy-MM-dd"); a meal added here is logged on it.
    let day: String
    /// Runs after a meal was added, edited or deleted.
    let onChanged: () async -> Void
    @State private var selected: TodayMeal?
    @State private var showAdd = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionHeader(title: "Refeições", systemImage: "fork.knife")
                Spacer()
                Button {
                    showAdd = true
                } label: {
                    Label("Adicionar", systemImage: "plus")
                        .font(.subheadline.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .buttonBorderShape(.capsule)
                .controlSize(.small)
                .tint(Palette.accent)
            }

            if meals.isEmpty {
                Text(day == MealText.isoDay()
                     ? "Ainda nada hoje. Toca em «Adicionar» para repetir uma refeição, ou regista-a pelo atalho."
                     : "Nenhuma refeição registada neste dia.")
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
            MealDetailSheet(meal: meal, onChanged: onChanged)
        }
        .sheet(isPresented: $showAdd) {
            AddMealView(day: day) {
                Task { await onChanged() }
            }
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
                if meal.hasPendingItems {
                    Label("A estimar um ingrediente…", systemImage: "hourglass")
                        .font(.caption)
                        .foregroundStyle(Palette.accentText)
                }
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

/// One logged meal: its photos, ingredients and note, with the two things to do
/// with it — edit it, or log it again today.
private struct MealDetailSheet: View {
    let meal: TodayMeal
    let onChanged: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var editor: MealEditorMode?
    @State private var nutrientTarget: NutrientTarget?

    /// An item tapped to see its full micronutrient profile. Identified by its
    /// position — names really are not unique, and less so now that they are
    /// translated: "potato" and "boiled potato" both read "batata".
    private struct NutrientTarget: Identifiable {
        let id: Int
        let item: MealItem
    }

    var body: some View {
        NavigationStack {
            List {
                let photos = meal.photoURLs
                if !photos.isEmpty {
                    Section {
                        PhotoStrip(urls: photos)
                    }
                }

                Section {
                    ForEach(Array(meal.items.enumerated()), id: \.offset) { index, item in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(MealText.sentence(item.name)).fontWeight(.medium)
                                Spacer()
                                if item.portionG > 0 {
                                    Text("\(MealText.grams(item.portionG)) g")
                                        .foregroundStyle(.secondary)
                                }
                            }
                            if item.isPending {
                                Label("A estimar…", systemImage: "hourglass")
                                    .font(.caption).foregroundStyle(.secondary)
                            } else if item.isFailed {
                                Label("Não deu para estimar", systemImage: "exclamationmark.triangle.fill")
                                    .font(.caption).foregroundStyle(Palette.warningText)
                            } else {
                                Text("\(Int(item.calories.rounded())) kcal · P \(Int(item.proteinG.rounded())) · H \(Int(item.carbsG.rounded())) · G \(Int(item.fatG.rounded()))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 2)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            nutrientTarget = NutrientTarget(id: index, item: item)
                        }
                    }
                } header: {
                    HStack(spacing: 4) {
                        Text("\(Int(meal.calories.rounded())) kcal · \(meal.time)")
                        if meal.edited {
                            Image(systemName: "pencil.circle.fill")
                                .accessibilityLabel("Editada na app")
                        }
                    }
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
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 12) {
                    Button {
                        editor = .edit(meal)
                    } label: {
                        Label("Editar", systemImage: "slider.horizontal.3")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    Button {
                        editor = .create(day: MealText.isoDay(), base: meal, versions: [])
                    } label: {
                        Label("Repetir hoje", systemImage: "arrow.triangle.2.circlepath")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                }
                .controlSize(.large)
                .tint(Palette.accent)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(.bar)
            }
        }
        .presentationDetents([.medium, .large])
        .sheet(item: $editor) { mode in
            NavigationStack {
                MealEditorView(mode: mode) {
                    // Close the editor and this sheet: the list behind is where the
                    // changed day shows, and it reloads now.
                    editor = nil
                    dismiss()
                    Task { await onChanged() }
                }
            }
        }
        .sheet(item: $nutrientTarget) { target in
            ItemNutrientSheet(item: target.item, title: MealText.sentence(target.item.name))
        }
    }
}

/// Horizontally scrolling photo strip for the meal log images.
private struct PhotoStrip: View {
    let urls: [URL]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(urls, id: \.self) { url in
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let img):
                            img.resizable().scaledToFill()
                                .frame(width: 240, height: 180)
                                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        case .failure:
                            Image(systemName: "photo.badge.exclamationmark")
                                .font(.title2).foregroundStyle(.secondary)
                                .frame(width: 240, height: 180)
                                .background(Palette.track, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        case .empty:
                            ProgressView()
                                .frame(width: 240, height: 180)
                        @unknown default:
                            EmptyView()
                        }
                    }
                }
            }
        }
        .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 4, trailing: 0))
    }
}

/// Drill-down for one ingredient: shows the full micronutrient profile of a single
/// food item — every catalogued nutrient listed with its amount. Tapping an item
/// in the meal detail sheet opens this instead of cluttering the list with inline
/// chips.
private struct ItemNutrientSheet: View {
    let item: MealItem
    let title: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("\(Int(item.portionG.rounded())) g")
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text("\(Int(item.calories.rounded())) kcal")
                                .font(.subheadline.weight(.semibold).monospacedDigit())
                        }
                        Text("P \(Int(item.proteinG.rounded()))g · H \(Int(item.carbsG.rounded()))g · G \(Int(item.fatG.rounded()))g")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                MicronutrientSection(nutrients: item.nutrients)
            }
            .navigationTitle(title)
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

/// Drill-down for a macro ring: shows which foods contributed the most of this macro.
private struct MacroDetailSheet: View {
    let response: TodayResponse
    let key: String
    let title: String
    let unit: String
    let color: Color
    @Environment(\.dismiss) private var dismiss

    private struct Contributor: Identifiable {
        let id = UUID()
        let name: String
        let time: String
        let amount: Double
    }

    private var contributors: [Contributor] {
        var out: [Contributor] = []
        for meal in response.meals {
            for item in meal.items {
                let amount: Double = {
                    switch key {
                    case "calories": return item.calories
                    case "protein_g": return item.proteinG
                    case "carbs_g": return item.carbsG
                    case "fat_g": return item.fatG
                    default: return 0
                    }
                }()
                if amount > 0 {
                    out.append(Contributor(name: item.name, time: meal.time, amount: amount))
                }
            }
        }
        return out.sorted { $0.amount > $1.amount }
    }

    private let macroUnit: String

    init(response: TodayResponse, key: String, title: String, unit: String, color: Color) {
        self.response = response
        self.key = key
        self.title = title
        self.unit = unit
        self.color = color
        // Calories are shown in kcal; macros in g.
        self.macroUnit = key == "calories" ? "kcal" : "g"
    }

    var body: some View {
        let contribs = contributors
        let consumed = response.consumed(key)
        let target = response.targets[key]
        let goal = target?.goal ?? 0
        let maxAmount = contribs.map(\.amount).max() ?? 1

        NavigationStack {
            List {
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(title).font(.headline)
                            Spacer()
                            Text("\(Int(consumed.rounded())) \(unit)")
                                .font(.system(size: 30, weight: .bold, design: .rounded))
                                .monospacedDigit()
                                .foregroundStyle(color)
                        }
                        TargetBar(fraction: goal > 0 ? consumed / goal : 0, fill: color, height: 10)
                        Text("de \(Int(goal.rounded())) \(unit) hoje")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                if contribs.isEmpty {
                    Section {
                        Text("Nenhum alimento de hoje contribuiu para \(title.lowercased()).")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Section("Origem hoje") {
                        ForEach(contribs) { c in
                            VStack(spacing: 6) {
                                HStack {
                                    Text(c.name.capitalized).font(.subheadline)
                                    Text(c.time).font(.caption).foregroundStyle(.tertiary)
                                    Spacer()
                                    Text("\(Int(c.amount.rounded()))")
                                        .font(.subheadline.monospacedDigit())
                                        .foregroundStyle(.secondary)
                                    Text(macroUnit)
                                        .font(.caption2).foregroundStyle(.tertiary)
                                }
                                TargetBar(fraction: maxAmount > 0 ? c.amount / maxAmount : 0,
                                          fill: color, height: 6)
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            }
            .navigationTitle(title)
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
