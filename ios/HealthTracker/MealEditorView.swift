//
//  MealEditorView.swift
//  HealthTracker
//
//  One editor for two jobs: changing a logged meal, and composing a new one from a
//  past meal. Either way the user sees the ingredients, changes grams with a
//  stepper (or by typing), removes one with a swipe, adds one from the history —
//  or describes a new one in words for the model — and saves once.
//
//  Every number on screen moves with the grams, micronutrients included, because
//  that is what the server records: /meals/save rescales each item from its own
//  values. The draft's arithmetic here only previews it.
//

import SwiftUI

/// What the editor is for.
enum MealEditorMode: Hashable, Identifiable {
    /// Change a logged meal in place.
    case edit(TodayMeal)
    /// Log a new meal on `day` ("yyyy-MM-dd"), starting from `base` (a past meal)
    /// or from nothing. `versions` are other takes on the same habit to switch to.
    case create(day: String, base: TodayMeal?, versions: [TodayMeal])

    var id: String {
        switch self {
        case .edit(let meal):
            return "edit-\(meal.datetime)"
        case .create(let day, let base, _):
            return "new-\(day)-\(base?.datetime ?? "blank")"
        }
    }
}

struct MealEditorView: View {
    let mode: MealEditorMode
    /// Presented as its own sheet (true) or pushed inside another (false) — only
    /// decides whether it shows its own "Cancelar".
    let isModal: Bool
    /// Called once the meal was saved or deleted. The presenter closes what it
    /// opened and reloads the day.
    let onFinished: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var draft: MealDraft
    @State private var initial: MealDraft
    @State private var time: Date
    @State private var selectedVersion: String?
    @State private var baseConfidence: Double
    /// One id per meal composed, so a retried save can never log it twice.
    @State private var clientID = UUID().uuidString
    @State private var isSaving = false
    @State private var errorMessage: String?
    @State private var showConflict = false
    @State private var confirmDelete = false
    @State private var showPicker = false
    @State private var inspected: Inspected?
    @FocusState private var focusedGrams: UUID?

    private struct Inspected: Identifiable { let id: UUID }

    init(mode: MealEditorMode, isModal: Bool = true, onFinished: @escaping () -> Void) {
        self.mode = mode
        self.isModal = isModal
        self.onFinished = onFinished
        let start: MealDraft
        var base: TodayMeal?
        switch mode {
        case .edit(let meal):
            start = MealDraft(items: meal.items)
        case .create(_, let source, _):
            base = source
            // Something still being estimated has no numbers to copy.
            start = MealDraft(items: (source?.items ?? []).filter { !$0.isPlaceholder })
        }
        _draft = State(initialValue: start)
        _initial = State(initialValue: start)
        _time = State(initialValue: Self.defaultTime(for: mode))
        _selectedVersion = State(initialValue: base?.datetime)
        _baseConfidence = State(initialValue: base?.confidence ?? 0.5)
    }

    private var isNew: Bool {
        if case .create = mode { return true }
        return false
    }

    private var canSave: Bool {
        !draft.isEmpty && (isNew || draft != initial) && !isSaving
    }

    var body: some View {
        List {
            Section {
                DraftTotalsView(totals: draft.totals, pending: draft.describedCount)
            }

            if case .create(let day, _, let versions) = mode {
                Section {
                    LabeledContent("Dia", value: MealText.dayLabel(day))
                    timePicker(day: day)
                }
                if versions.count > 1 {
                    Section("Outras vezes que comeste isto") {
                        versionStrip(versions)
                    }
                }
            }

            Section {
                ForEach($draft.items) { $item in
                    IngredientRow(item: $item, focus: $focusedGrams) {
                        inspected = Inspected(id: item.id)
                    }
                }
                .onDelete { draft.items.remove(atOffsets: $0) }

                Button {
                    showPicker = true
                } label: {
                    Label("Adicionar ingrediente", systemImage: "plus.circle.fill")
                        .fontWeight(.medium)
                }
            } header: {
                Text("Ingredientes")
            } footer: {
                Text(draft.isEmpty && !isNew
                     ? "Para tirar a refeição do registo, usa «Apagar refeição»."
                     : "Muda as gramas e tudo é recalculado, micronutrientes incluídos. "
                       + "Toca num ingrediente para ver o detalhe; desliza para o remover.")
            }

            if case .edit = mode {
                Section {
                    Button(role: .destructive) {
                        confirmDelete = true
                    } label: {
                        Label("Apagar refeição", systemImage: "trash")
                    }
                }
            }

            if let errorMessage {
                Section {
                    Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(Palette.criticalText)
                }
            }
        }
        .animation(.snappy, value: draft.items.count)
        .scrollDismissesKeyboard(.interactively)
        .navigationTitle(isNew ? "Nova refeição" : "Editar refeição")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if isModal {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancelar") { dismiss() }
                }
            }
            ToolbarItem(placement: .confirmationAction) {
                if isSaving {
                    ProgressView()
                } else {
                    Button(isNew ? "Registar" : "Guardar") {
                        Task { await save() }
                    }
                    .fontWeight(.semibold)
                    .disabled(!canSave)
                }
            }
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("OK") { focusedGrams = nil }
            }
        }
        .disabled(isSaving)
        .interactiveDismissDisabled(draft != initial)
        .sheet(isPresented: $showPicker) {
            NavigationStack {
                IngredientPicker { picked in
                    draft.items.append(picked)
                }
            }
        }
        .sheet(item: $inspected) { target in
            if let item = draft.items.first(where: { $0.id == target.id }) {
                DraftItemSheet(item: item) { updated in
                    if let index = draft.items.firstIndex(where: { $0.id == updated.id }) {
                        draft.items[index] = updated
                    }
                } onRemove: {
                    inspected = nil
                    draft.items.removeAll { $0.id == target.id }
                }
            }
        }
        .confirmationDialog("Apagar esta refeição?", isPresented: $confirmDelete,
                            titleVisibility: .visible) {
            Button("Apagar refeição", role: .destructive) {
                Task { await delete() }
            }
        } message: {
            Text("Sai do registo e dos totais do dia.")
        }
        .alert("Esta refeição mudou entretanto", isPresented: $showConflict) {
            Button("OK") { onFinished() }
        } message: {
            Text("Um ingrediente acabou de ser estimado. Abre-a de novo para editares a versão atual.")
        }
    }

    // MARK: - Pieces

    @ViewBuilder
    private func timePicker(day: String) -> some View {
        if day == MealText.isoDay() {
            DatePicker("Hora", selection: $time, in: ...Date(),
                       displayedComponents: .hourAndMinute)
        } else {
            DatePicker("Hora", selection: $time, displayedComponents: .hourAndMinute)
        }
    }

    private func versionStrip(_ versions: [TodayMeal]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(versions) { version in
                    let selected = version.datetime == selectedVersion
                    Button {
                        withAnimation(.snappy) { apply(version) }
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(MealText.dayLabel(version.datetime))
                                .font(.subheadline.weight(.semibold))
                            Text("\(Int(version.calories.rounded())) kcal · \(version.items.count) ingr.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(selected ? Palette.accent.opacity(0.14) : Palette.track,
                                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay {
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .strokeBorder(selected ? Palette.accent : .clear, lineWidth: 1.5)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 2)
        }
        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
    }

    private func apply(_ version: TodayMeal) {
        draft = MealDraft(items: version.items.filter { !$0.isPlaceholder })
        selectedVersion = version.datetime
        baseConfidence = version.confidence
    }

    // MARK: - Actions

    private func save() async {
        focusedGrams = nil
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        var body = draft.body()
        switch mode {
        case .edit(let meal):
            body["datetime"] = meal.datetime
            if let rev = meal.rev { body["rev"] = rev }
        case .create(let day, _, _):
            body["client_id"] = clientID
            body["date"] = day
            body["time"] = Self.hhmm(time)
            body["confidence"] = baseConfidence
        }
        do {
            try await APIClient.shared.saveMeal(body)
            onFinished()
        } catch APIError.badStatus(409) {
            showConflict = true
        } catch {
            errorMessage = "Não deu para guardar: \(error.localizedDescription)"
        }
    }

    private func delete() async {
        guard case .edit(let meal) = mode else { return }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            try await APIClient.shared.deleteMeal(datetime: meal.datetime)
            onFinished()
        } catch {
            errorMessage = "Não deu para apagar: \(error.localizedDescription)"
        }
    }

    // MARK: - Time

    /// Now, for a meal logged today. For an earlier day, the hour the source meal
    /// was eaten at — or lunchtime, when starting from nothing.
    private static func defaultTime(for mode: MealEditorMode) -> Date {
        guard case .create(let day, let base, _) = mode, day != MealText.isoDay() else {
            return Date()
        }
        let parts = (base?.time ?? "13:00").split(separator: ":").compactMap { Int($0) }
        return Calendar.current.date(bySettingHour: parts.first ?? 13,
                                     minute: parts.count > 1 ? parts[1] : 0,
                                     second: 0, of: Date()) ?? Date()
    }

    private static func hhmm(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}

// MARK: - Totals

private struct DraftTotalsView: View {
    let totals: MacroValues
    let pending: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("\(Int(totals.calories.rounded()))")
                    .font(.system(size: 44, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText(value: totals.calories))
                Text("kcal")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            HStack(spacing: 8) {
                MacroChip(title: "Proteína", grams: totals.proteinG, color: Palette.protein)
                MacroChip(title: "Hidratos", grams: totals.carbsG, color: Palette.carbs)
                MacroChip(title: "Gordura", grams: totals.fatG, color: Palette.fat)
            }
            if pending > 0 {
                Label(pending == 1 ? "Mais 1 ingrediente a estimar pela IA"
                                   : "Mais \(pending) ingredientes a estimar pela IA",
                      systemImage: "sparkles")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 6)
        .animation(.snappy, value: totals)
    }
}

private struct MacroChip: View {
    let title: String
    let grams: Double
    let color: Color

    var body: some View {
        VStack(spacing: 2) {
            Text("\(Int(grams.rounded())) g")
                .font(.headline.monospacedDigit())
                .foregroundStyle(color)
                .contentTransition(.numericText(value: grams))
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

// MARK: - One ingredient in the list

private struct IngredientRow: View {
    @Binding var item: DraftItem
    let focus: FocusState<UUID?>.Binding
    let onInspect: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(MealText.sentence(item.name))
                    .fontWeight(.medium)
                    .lineLimit(2)
                subtitle
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
            .onTapGesture(perform: onInspect)

            if item.isScalable {
                GramsField(item: $item, focus: focus)
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private var subtitle: some View {
        if item.isDescribed {
            Label("Estimado pela IA quando guardares", systemImage: "sparkles")
                .font(.caption)
                .foregroundStyle(Palette.accentText)
        } else if item.base.isPending {
            Label("A estimar…", systemImage: "hourglass")
                .font(.caption)
                .foregroundStyle(.secondary)
        } else if item.base.isFailed {
            Label("Não deu para estimar — remove-o ou descreve-o de novo",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundStyle(Palette.warningText)
        } else {
            let m = item.macros
            HStack(spacing: 4) {
                Text("\(Int(m.calories.rounded())) kcal · P \(Int(m.proteinG.rounded())) · H \(Int(m.carbsG.rounded())) · G \(Int(m.fatG.rounded()))")
                    .contentTransition(.numericText())
                if item.isCorrected {
                    Image(systemName: "pencil.circle.fill")
                        .foregroundStyle(Palette.accentText)
                        .accessibilityLabel("Valores corrigidos à mão")
                }
            }
            .font(.caption)
            .monospacedDigit()
            .foregroundStyle(.secondary)
        }
    }
}

/// − [ 55 ] g + : the grams of one ingredient. Steps are sized to the amount (1 g
/// under 20 g, 5 g under 100 g, 10 g above), and typing a number applies as you go.
private struct GramsField: View {
    @Binding var item: DraftItem
    let focus: FocusState<UUID?>.Binding
    @State private var text = ""

    var body: some View {
        HStack(spacing: 4) {
            stepButton(symbol: "minus.circle.fill", label: "Menos", up: false)
            TextField("0", text: $text)
                .keyboardType(.decimalPad)
                .multilineTextAlignment(.trailing)
                .font(.body.monospacedDigit().weight(.semibold))
                .frame(width: 50)
                .focused(focus, equals: item.id)
                .onChange(of: text) { _, new in
                    // Only what the user types — never the echo of a stepper tap or
                    // of the initial value, which must not mark the item as changed.
                    guard focus.wrappedValue == item.id,
                          let value = Double(new.replacingOccurrences(of: ",", with: ".")),
                          value > 0 else { return }
                    item.setGrams(value)
                }
            Text("g")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            stepButton(symbol: "plus.circle.fill", label: "Mais", up: true)
        }
        .onAppear { text = MealText.grams(item.grams) }
        .onChange(of: item.grams) { _, grams in
            if focus.wrappedValue != item.id { text = MealText.grams(grams) }
        }
    }

    private func stepButton(symbol: String, label: String, up: Bool) -> some View {
        Button {
            let step = Self.step(for: item.grams)
            let units = up ? (item.grams / step).rounded(.down) + 1
                           : (item.grams / step).rounded(.up) - 1
            let next = max(step, units * step)
            item.setGrams(next)
            text = MealText.grams(next)
        } label: {
            Image(systemName: symbol)
                .font(.title2)
                .symbolRenderingMode(.hierarchical)
        }
        .buttonStyle(.borderless)
        .tint(Palette.accent)
        .accessibilityLabel(label)
        .sensoryFeedback(.selection, trigger: item.grams)
    }

    private static func step(for grams: Double) -> Double {
        grams < 20 ? 1 : grams < 100 ? 5 : 10
    }
}

// MARK: - One ingredient in detail

/// Grams, the numbers at that portion, a hand correction for when the estimate
/// was wrong, and the micronutrients. Works on a copy and reports every change, so
/// the list behind it is always current and a removal can't pull the row out from
/// under a binding.
private struct DraftItemSheet: View {
    @State private var item: DraftItem
    let onUpdate: (DraftItem) -> Void
    let onRemove: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var correcting: Bool
    @State private var fields: [String]
    @FocusState private var focus: UUID?

    private static let labels = [("Calorias", "kcal"), ("Proteína", "g"),
                                 ("Hidratos", "g"), ("Gordura", "g")]

    init(item: DraftItem, onUpdate: @escaping (DraftItem) -> Void,
         onRemove: @escaping () -> Void) {
        _item = State(initialValue: item)
        _correcting = State(initialValue: item.isCorrected)
        _fields = State(initialValue: Self.text(item.macros))
        self.onUpdate = onUpdate
        self.onRemove = onRemove
    }

    var body: some View {
        NavigationStack {
            List {
                if item.isScalable {
                    Section {
                        HStack {
                            Text("Quantidade")
                            Spacer()
                            GramsField(item: $item, focus: $focus)
                        }
                    } footer: {
                        Text("Calorias, macros e micronutrientes acompanham a quantidade.")
                    }
                }

                let m = item.macros
                Section("Nesta porção") {
                    LabeledContent("Calorias", value: "\(Int(m.calories.rounded())) kcal")
                    LabeledContent("Proteína", value: "\(Int(m.proteinG.rounded())) g")
                    LabeledContent("Hidratos", value: "\(Int(m.carbsG.rounded())) g")
                    LabeledContent("Gordura", value: "\(Int(m.fatG.rounded())) g")
                }
                .monospacedDigit()

                if !item.isDescribed && !item.base.isPlaceholder {
                    Section {
                        Toggle("Corrigir valores à mão", isOn: $correcting.animation())
                        if correcting {
                            ForEach(Self.labels.indices, id: \.self) { index in
                                HStack {
                                    Text(Self.labels[index].0)
                                    Spacer()
                                    TextField("0", text: $fields[index])
                                        .keyboardType(.decimalPad)
                                        .multilineTextAlignment(.trailing)
                                        .frame(width: 80)
                                    Text(Self.labels[index].1)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    } footer: {
                        Text("Para quando a estimativa está errada — por exemplo, se o rótulo diz outra coisa. Os micronutrientes não mudam.")
                    }
                }

                MicronutrientSection(nutrients: item.nutrients)

                Section {
                    Button(role: .destructive, action: onRemove) {
                        Label("Remover ingrediente", systemImage: "minus.circle")
                    }
                }
            }
            .navigationTitle(MealText.sentence(item.name))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("OK") { dismiss() }
                        .fontWeight(.semibold)
                }
            }
        }
        .presentationDetents([.medium, .large])
        .onChange(of: correcting) { _, on in
            if on {
                fields = Self.text(item.macros)
            } else {
                item.correct(nil)
            }
        }
        .onChange(of: fields) { _, _ in
            guard correcting else { return }
            let current = item.macros
            let values = fields.map { Double($0.replacingOccurrences(of: ",", with: ".")) }
            item.correct(MacroValues(calories: values[0] ?? current.calories,
                                     proteinG: values[1] ?? current.proteinG,
                                     carbsG: values[2] ?? current.carbsG,
                                     fatG: values[3] ?? current.fatG))
        }
        .onChange(of: item.grams) { _, _ in
            // A correction scales with the portion; keep its fields in step.
            if correcting { fields = Self.text(item.macros) }
        }
        .onChange(of: item) { _, updated in onUpdate(updated) }
    }

    private static func text(_ m: MacroValues) -> [String] {
        [m.calories, m.proteinG, m.carbsG, m.fatG].map { MealText.grams(($0 * 10).rounded() / 10) }
    }
}

/// Every catalogued micronutrient in `nutrients`, by name — shared by the editor's
/// item sheet and the meal detail's.
struct MicronutrientSection: View {
    let nutrients: [String: Double]

    var body: some View {
        let rows = nutrients
            .filter { $0.value > 0 }
            .compactMap { (key, value) -> (NutrientDef, Double)? in
                NutrientCatalog.byKey[key].map { ($0, value) }
            }
            .sorted { $0.0.label < $1.0.label }
        if rows.isEmpty {
            Section {
                Text("Este alimento não tem micronutrientes registados.")
                    .foregroundStyle(.secondary)
            }
        } else {
            Section("Micronutrientes") {
                ForEach(rows, id: \.0.id) { def, value in
                    HStack {
                        Text(def.label)
                            .font(.subheadline)
                        Spacer()
                        Text(def.amount(value))
                            .font(.subheadline.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Adding an ingredient

/// Every food the user has logged, most eaten first, searchable — plus, for a food
/// the history doesn't have, "estimate this" with the model. Typing grams in the
/// search ("banana 120 g") sets them on the ingredient picked.
struct IngredientPicker: View {
    let onPick: (DraftItem) -> Void

    @Environment(\.dismiss) private var dismiss
    private let store = MealLibraryStore.shared
    @State private var query = ""

    private var trimmed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        let all = store.library?.ingredients ?? []
        let matches = trimmed.isEmpty ? all : all.filter {
            MealText.matches("\($0.item.name) \($0.item.key)", query: trimmed)
        }
        List {
            if !matches.isEmpty {
                Section(trimmed.isEmpty ? "Os que mais comes" : "No teu histórico") {
                    ForEach(matches) { ingredient in
                        Button { pick(ingredient) } label: {
                            IngredientLabel(ingredient: ingredient)
                        }
                        .tint(.primary)
                    }
                }
            }
            if !trimmed.isEmpty {
                Section {
                    Button {
                        onPick(DraftItem(describing: trimmed))
                        dismiss()
                    } label: {
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Estimar «\(trimmed)»")
                                    .foregroundStyle(.primary)
                                Text("A IA calcula os valores depois de guardares")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: "sparkles")
                                .foregroundStyle(Palette.accent)
                        }
                    }
                } footer: {
                    Text("Não está no histórico? Escreve o alimento e a quantidade — por exemplo, «banana 120 g».")
                }
            }
        }
        .overlay {
            if all.isEmpty && store.isLoading {
                ProgressView()
            }
        }
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always),
                    prompt: "Procurar ou descrever um alimento")
        .navigationTitle("Adicionar ingrediente")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancelar") { dismiss() }
            }
        }
        .task { await store.load() }
    }

    private func pick(_ ingredient: LibraryIngredient) {
        var item = DraftItem(item: ingredient.item)
        if let grams = MealText.grams(in: trimmed) { item.setGrams(grams) }
        onPick(item)
        dismiss()
    }
}

private struct IngredientLabel: View {
    let ingredient: LibraryIngredient

    var body: some View {
        let item = ingredient.item
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(MealText.sentence(item.name))
                    .foregroundStyle(.primary)
                Text((item.portionG > 0 ? "\(MealText.grams(item.portionG)) g · " : "")
                     + "\(Int(item.calories.rounded())) kcal · P \(Int(item.proteinG.rounded()))")
                    .font(.caption)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text("\(ingredient.count)×")
                .font(.caption.monospacedDigit())
                .foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }
}
