//
//  MealLibrary.swift
//  HealthTracker
//
//  Logging a meal again, and editing one for real.
//
//  There are no templates any more. A template was a frozen copy of one meal, and
//  breakfast is the same meal most days but never the same grams, so frozen copies
//  were abandoned. The history is the library instead: GET /meals/library returns
//  the user's habits (found by the server from what was actually eaten), the recent
//  meals and every ingredient ever logged. Picking one gives a `MealDraft` — the
//  meal's ingredients, each editable — which is saved in one call.
//
//  The arithmetic here mirrors the server's (`meal_library.rescale`): a new portion
//  scales every macro AND every micronutrient in proportion. The draft only uses
//  it to preview; the server rescales from the same base values and is the one that
//  writes, so the preview can never drift into the sheet.
//

import Foundation
import Observation

// MARK: - GET /meals/library

struct MealLibrary: Decodable {
    let suggestions: [MealHabit]
    let recent: [TodayMeal]
    let ingredients: [LibraryIngredient]

    enum CodingKeys: String, CodingKey { case suggestions, recent, ingredients }

    /// Every meal the library knows, newest first and without repeats — what the
    /// search runs over (recent meals plus the older versions of each habit).
    var allMeals: [TodayMeal] {
        var seen = Set<String>()
        return (recent + suggestions.flatMap(\.versions))
            .filter { seen.insert($0.datetime).inserted }
            .sorted { $0.datetime > $1.datetime }
    }
}

/// A meal the user keeps coming back to. `meal` is its latest version — the best
/// guess at today's — and `versions` the distinct recent ones, latest first.
struct MealHabit: Decodable, Identifiable, Hashable {
    let id: String
    let meal: TodayMeal
    let versions: [TodayMeal]
    let count: Int
    let typicalTime: String

    enum CodingKeys: String, CodingKey {
        case id, meal, versions, count
        case typicalTime = "typical_time"
    }
}

/// One food from the history, as it was last logged.
struct LibraryIngredient: Decodable, Identifiable, Hashable {
    let key: String
    let count: Int
    let last: String
    let item: MealItem

    var id: String { key }

    enum CodingKeys: String, CodingKey { case key, count, last, item }
}

/// What /meals/save answers with. Only the id is relied on — the day is reloaded
/// after every save — so a response the full `TodayMeal` decoder would reject can
/// never turn a saved meal into an error on screen.
struct SavedMeal: Decodable {
    let datetime: String
}

struct DeletedMeal: Decodable {
    let deleted: Bool
    let datetime: String
}

// MARK: - Store

/// The library, fetched when the add-meal sheet opens and cached on disk, so the
/// sheet shows last time's list at once and refreshes behind it.
@MainActor
@Observable
final class MealLibraryStore {
    static let shared = MealLibraryStore()

    var library: MealLibrary?
    var errorMessage: String?
    var isLoading = false

    @ObservationIgnored private var inFlight: Task<Void, Never>?

    init() {
        library = APIClient.shared.cachedMealLibrary()
    }

    /// Reload, coalescing concurrent callers onto one request (the add-meal sheet
    /// and the ingredient picker can both ask at once).
    func load() async {
        if let existing = inFlight {
            await existing.value
            return
        }
        let task = Task { @MainActor in
            defer { self.inFlight = nil }
            self.isLoading = self.library == nil
            defer { self.isLoading = false }
            do {
                self.library = try await APIClient.shared.mealLibrary()
                self.errorMessage = nil
            } catch {
                if self.library == nil { self.errorMessage = error.localizedDescription }
            }
        }
        inFlight = task
        await task.value
    }
}

// MARK: - The draft being edited

/// Calories and the three macros — one item's, or a meal's total.
struct MacroValues: Hashable {
    var calories: Double
    var proteinG: Double
    var carbsG: Double
    var fatG: Double

    static let zero = MacroValues(calories: 0, proteinG: 0, carbsG: 0, fatG: 0)

    init(calories: Double, proteinG: Double, carbsG: Double, fatG: Double) {
        self.calories = calories
        self.proteinG = proteinG
        self.carbsG = carbsG
        self.fatG = fatG
    }

    init(_ item: MealItem) {
        self.init(calories: item.calories, proteinG: item.proteinG,
                  carbsG: item.carbsG, fatG: item.fatG)
    }

    func scaled(_ factor: Double) -> MacroValues {
        MacroValues(calories: calories * factor, proteinG: proteinG * factor,
                    carbsG: carbsG * factor, fatG: fatG * factor)
    }

    static func + (lhs: MacroValues, rhs: MacroValues) -> MacroValues {
        MacroValues(calories: lhs.calories + rhs.calories,
                    proteinG: lhs.proteinG + rhs.proteinG,
                    carbsG: lhs.carbsG + rhs.carbsG, fatG: lhs.fatG + rhs.fatG)
    }

    /// The shape /meals/save takes for a hand-typed correction.
    var payload: [String: Any] {
        ["calories": calories, "protein_g": proteinG, "carbs_g": carbsG, "fat_g": fatG]
    }
}

/// One ingredient in the editor: the item as served (`base`, never modified) plus
/// what the user changed — its grams and, rarely, hand-typed macros.
struct DraftItem: Identifiable, Hashable {
    let id = UUID()
    let base: MealItem
    private(set) var grams: Double
    /// Hand-typed macros for the CURRENT grams (a label said otherwise). Scaled
    /// along if the grams change afterwards, since a correction is a density.
    private(set) var override: MacroValues?
    /// Written in words, for the model to estimate on save ("banana 120 g").
    let isDescribed: Bool

    init(item: MealItem) {
        base = item
        grams = item.portionG
        isDescribed = false
    }

    init(describing text: String) {
        base = MealItem(name: text, key: text, portionG: 0, calories: 0, proteinG: 0,
                        carbsG: 0, fatG: 0)
        grams = 0
        isDescribed = true
    }

    var name: String { base.name }

    /// Only a real, weighed item has a per-gram basis to scale from.
    var isScalable: Bool { base.portionG > 0 && !base.isPlaceholder && !isDescribed }

    private var factor: Double { isScalable ? grams / base.portionG : 1 }

    var macros: MacroValues { override ?? MacroValues(base).scaled(factor) }

    /// The micronutrients at the current grams — a hand correction can't reach them.
    var nutrients: [String: Double] { base.nutrients.mapValues { $0 * factor } }

    var isPortionChanged: Bool { isScalable && abs(grams - base.portionG) >= 0.05 }
    var isCorrected: Bool { override != nil }

    mutating func setGrams(_ value: Double) {
        guard isScalable, value > 0, value.isFinite else { return }
        if let override, grams > 0 { self.override = override.scaled(value / grams) }
        grams = value
    }

    mutating func correct(_ values: MacroValues?) { override = values }

    /// This item as one entry of /meals/save's `items`.
    var payload: [String: Any] {
        var entry: [String: Any] = ["base": base.payload]
        if isPortionChanged { entry["portion_g"] = (grams * 10).rounded() / 10 }
        if let override { entry["override"] = override.payload }
        return entry
    }
}

/// A whole meal being edited — or composed from a past one.
struct MealDraft: Hashable {
    var items: [DraftItem]

    init(items: [MealItem] = []) {
        self.items = items.map(DraftItem.init(item:))
    }

    /// What the meal adds up to now. Ingredients still to be estimated count zero
    /// until they are; `describedCount` says how many.
    var totals: MacroValues { items.map(\.macros).reduce(.zero, +) }
    var describedCount: Int { items.filter { $0.isDescribed || $0.base.isPending }.count }
    var isEmpty: Bool { items.isEmpty }

    /// The /meals/save body for these items. Described ingredients travel as one
    /// `describe` text; the model splits it into items itself.
    func body() -> [String: Any] {
        var out: [String: Any] = [
            "items": items.filter { !$0.isDescribed }.map(\.payload),
        ]
        let described = items.filter(\.isDescribed).map(\.name).joined(separator: ", ")
        if !described.isEmpty { out["describe"] = described }
        return out
    }
}

// MARK: - Small shared helpers

enum MealText {
    /// "Aveia em flocos" from "aveia em flocos": the item names are stored in lower
    /// case, and `.capitalized` would shout every word.
    static func sentence(_ text: String) -> String {
        text.prefix(1).uppercased() + text.dropFirst()
    }

    /// Case- and accent-insensitive, so "acucar" finds "açúcar".
    static func folded(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive],
                     locale: Locale(identifier: "pt_PT"))
    }

    /// Whether every word of `query` (ignoring numbers — "banana 120 g" searches
    /// for "banana") appears in `text`.
    static func matches(_ text: String, query: String) -> Bool {
        let haystack = folded(text)
        let words = folded(query).split(whereSeparator: { !$0.isLetter })
            .filter { $0.count > 1 && $0 != "g" }
        return !words.isEmpty && words.allSatisfy { haystack.contains($0) }
    }

    /// Grams written in a search ("banana 120 g", "120g de banana"), if any.
    static func grams(in query: String) -> Double? {
        guard let match = query.firstMatch(of: #/(\d+(?:[.,]\d+)?)\s*(?:g|gr|gramas)\b/#)
        else { return nil }
        return Double(match.1.replacingOccurrences(of: ",", with: "."))
    }

    /// A "13 set" style label for a meal's day — "Hoje"/"Ontem" when it is.
    static func dayLabel(_ iso: String, today: Date = Date()) -> String {
        let parser = DateFormatter()
        parser.calendar = Calendar(identifier: .gregorian)
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: String(iso.prefix(10))) else { return iso }
        let calendar = Calendar.current
        if calendar.isDate(date, inSameDayAs: today) { return "Hoje" }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: today),
           calendar.isDate(date, inSameDayAs: yesterday) { return "Ontem" }
        let out = DateFormatter()
        out.locale = Locale(identifier: "pt_PT")
        out.dateFormat = calendar.isDate(date, equalTo: today, toGranularity: .year)
            ? "EEE, d MMM" : "d MMM yyyy"
        return sentence(out.string(from: date).replacingOccurrences(of: ".", with: ""))
    }

    /// Today as the API writes a day.
    static func isoDay(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }

    /// A gram amount without a pointless ".0".
    static func grams(_ value: Double) -> String {
        value.rounded() == value || value >= 100
            ? String(Int(value.rounded()))
            : String(format: "%.1f", value).replacingOccurrences(of: ".", with: ",")
    }
}
