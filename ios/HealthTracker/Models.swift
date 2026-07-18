//
//  Models.swift
//  HealthTracker
//
//  The shapes returned by GET /today — the live daily payload: what has been eaten
//  so far, the target for every metric, and the meals (with per-ingredient
//  nutrients, so any nutrient can be traced back to the foods that supplied it).
//
//  Decoded WITHOUT .convertFromSnakeCase (see APIClient): that strategy also
//  rewrites the keys of a [String: T] dictionary, which would silently turn
//  "protein_g" into "proteinG" inside `consumed`/`targets`/`nutrients` and break
//  every lookup. Instead each struct declares explicit CodingKeys and the dynamic
//  dictionary keys stay exactly as the backend sent them (matching NutrientCatalog).
//

import Foundation

// MARK: - GET /today

struct TodayResponse: Decodable {
    let date: String
    let mealCount: Int
    /// metric key -> amount eaten so far today (macros always present; a micro only
    /// when non-zero). Keys are the backend's snake_case, e.g. "protein_g".
    let consumed: [String: Double]
    /// metric key -> its goal. Covers macros (measured) and every micro (rda).
    let targets: [String: Target]
    let basis: Basis
    let meals: [TodayMeal]

    enum CodingKeys: String, CodingKey {
        case date, consumed, targets, basis, meals
        case mealCount = "meal_count"
    }

    func consumed(_ key: String) -> Double { consumed[key] ?? 0 }
}

/// A per-metric goal. `kind` decides how to read floor/ceiling and how to colour it.
struct Target: Decodable, Hashable {
    let kind: String        // Kind.reach / .limit / .window
    let floor: Double?      // reach: hit this. window: lower edge.
    let ceiling: Double?    // limit: stay under this. window: upper edge.
    let unit: String
    let source: String?     // "measured" | "rda" | "manual"

    enum Kind {
        static let reach = "reach"
        static let limit = "limit"
        static let window = "window"
    }

    /// The single number a ring/bar fills toward: the floor for reach, the ceiling
    /// for a limit, the mid-point of a window.
    var goal: Double {
        switch kind {
        case Kind.limit:  return ceiling ?? floor ?? 0
        case Kind.window: return ((floor ?? 0) + (ceiling ?? floor ?? 0)) / 2
        default:          return floor ?? ceiling ?? 0
        }
    }
}

/// The inputs the measured targets were derived from — shown in Profile so the
/// numbers are never a black box.
struct Basis: Decodable, Hashable {
    let tdeeKcal: Double?
    let calorieTargetKcal: Double?
    let weightKg: Double?
    let leanMassKg: Double?
    let proteinGPerKg: Double?
    let calorieDeficitPct: Double?
    let goal: String?

    enum CodingKeys: String, CodingKey {
        case tdeeKcal = "tdee_kcal"
        case calorieTargetKcal = "calorie_target_kcal"
        case weightKg = "weight_kg"
        case leanMassKg = "lean_mass_kg"
        case proteinGPerKg = "protein_g_per_kg"
        case calorieDeficitPct = "calorie_deficit_pct"
        case goal
    }
}

struct TodayMeal: Decodable, Identifiable, Hashable {
    let datetime: String
    let time: String
    let foods: String
    let note: String
    let template: String
    let calories: Double
    let proteinG: Double
    let carbsG: Double
    let fatG: Double
    let items: [MealItem]

    // datetime is unique per meal (down to the second) — a stable list identity.
    var id: String { datetime }

    enum CodingKeys: String, CodingKey {
        case datetime, time, foods, note, template, calories, items
        case proteinG = "protein_g"
        case carbsG = "carbs_g"
        case fatG = "fat_g"
    }
}

/// One ingredient of a meal, carrying its own `nutrients` map — the raw material
/// for the "which foods gave me this nutrient?" drill-down.
struct MealItem: Decodable, Hashable, Identifiable {
    let name: String
    let portionG: Double
    let calories: Double
    let proteinG: Double
    let carbsG: Double
    let fatG: Double
    let nutrients: [String: Double]

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, calories, nutrients
        case portionG = "portion_g"
        case proteinG = "protein_g"
        case carbsG = "carbs_g"
        case fatG = "fat_g"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        portionG = try c.decodeIfPresent(Double.self, forKey: .portionG) ?? 0
        calories = try c.decodeIfPresent(Double.self, forKey: .calories) ?? 0
        proteinG = try c.decodeIfPresent(Double.self, forKey: .proteinG) ?? 0
        carbsG = try c.decodeIfPresent(Double.self, forKey: .carbsG) ?? 0
        fatG = try c.decodeIfPresent(Double.self, forKey: .fatG) ?? 0
        // `nutrients` is omitted for a trace-free food — default to empty.
        nutrients = try c.decodeIfPresent([String: Double].self, forKey: .nutrients) ?? [:]
    }
}
