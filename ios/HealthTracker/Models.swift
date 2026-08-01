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
    /// The last few completed days of intake (see /today `history`). Optional so an
    /// older payload still decodes; use `historyDays`. Powers the rolling-average and
    /// week-consistency lenses on the Nutrients screen.
    let history: [DayIntake]?
    /// What this user actually measures. The app draws itself from this rather than
    /// from a build-time constant, so turning a block on in the sheet changes the UI
    /// on the next fetch with no release. Optional so an older cached payload still
    /// decodes — `caps` falls back to "everything", which is what every build before
    /// this one assumed.
    let capabilities: Capabilities?

    enum CodingKeys: String, CodingKey {
        case date, consumed, targets, basis, meals, history, capabilities
        case mealCount = "meal_count"
    }

    var caps: Capabilities { capabilities ?? .full }

    func consumed(_ key: String) -> Double { consumed[key] ?? 0 }

    /// The rolling window, oldest day first, empty when the backend sent none.
    var historyDays: [DayIntake] { history ?? [] }
}

/// One completed day from the rolling `history` window — the same shape as `consumed`,
/// so a nutrient's multi-day pattern reads exactly like today's intake.
struct DayIntake: Decodable, Hashable {
    let date: String
    let consumed: [String: Double]

    enum CodingKeys: String, CodingKey { case date, consumed }

    func consumed(_ key: String) -> Double { consumed[key] ?? 0 }
}

/// A per-metric goal. `kind` decides how to read floor/ceiling and how to colour it;
/// `horizon` decides whether it's judged on today or on a rolling average; a `ceiling`
/// on a non-limit metric is that nutrient's toxicity upper limit (UL).
struct Target: Decodable, Hashable {
    let kind: String        // Kind.reach / .limit / .window
    let floor: Double?      // reach: hit this. window: lower edge.
    let ceiling: Double?    // limit: stay under this. window: upper edge. reach: the UL.
    let unit: String
    let source: String?     // "fixed" | "rda"
    let horizon: String?    // Horizon.daily / .rolling; nil on older payloads

    enum Kind {
        static let reach = "reach"
        static let limit = "limit"
        static let window = "window"
    }

    enum Horizon {
        static let daily = "daily"      // non-cumulative — judge today; consistency matters
        static let rolling = "rolling"  // body-banked — judge the multi-day average
    }

    /// Buffered by body stores, so a single low day is covered by reserves — read it
    /// against the rolling average, not today. Defaults to daily when unknown.
    var isRolling: Bool { horizon == Horizon.rolling }

    /// A reach/window nutrient may also carry a toxicity ceiling (its UL). This is that
    /// UL, or nil when a surplus is biologically safe (and so must never read as a
    /// risk). For a pure `limit` the ceiling IS the target, not a UL, so it's excluded.
    var upperLimit: Double? { kind == Kind.limit ? nil : ceiling }

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

/// The fixed daily plan plus the latest measured biometrics — shown in Profile.
struct Basis: Decodable, Hashable {
    let calorieTargetKcal: Double?
    let weightKg: Double?
    let leanMassKg: Double?

    enum CodingKeys: String, CodingKey {
        case calorieTargetKcal = "calorie_target_kcal"
        case weightKg = "weight_kg"
        case leanMassKg = "lean_mass_kg"
    }
}

/// What this user measures — the one switch the whole app reads. See
/// `schema/capabilities.py`: a set of blocks, deliberately not a level, because
/// someone with a watch but no scale is not "level 2.5".
struct Capabilities: Decodable, Hashable {
    let blocks: [String]
    let domains: [String]
    let blindSpots: [String]

    enum CodingKeys: String, CodingKey {
        case blocks, domains
        case blindSpots = "blind_spots"
    }

    /// Every block on — what every build before capabilities existed assumed, and
    /// the right fallback for a payload that predates them.
    static let full = Capabilities(
        blocks: ["self_report", "sleep", "recovery", "activity", "nutrition", "body"],
        domains: ["nutrition", "sleep", "activity", "body", "digestion"],
        blindSpots: [])

    func has(_ block: String) -> Bool { blocks.contains(block) }

    /// True when there is any measured body/activity data at all — the gate for the
    /// whole Trends screen, which is otherwise three empty charts.
    var hasAnyMetrics: Bool {
        has("body") || has("activity") || has("sleep") || has("recovery")
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
    let photoUrl: String?
    /// True once a user has hand-corrected an item via /meals/edit. Absent on an
    /// older cached payload, so it defaults to false rather than failing to decode.
    let edited: Bool
    let items: [MealItem]

    // datetime is unique per meal (down to the second) — a stable list identity.
    var id: String { datetime }

    /// Space-separated photo URLs from the backend (one per image uploaded).
    /// Google Drive webViewLinks are converted to direct thumbnail URLs
    /// so AsyncImage can render them.
    var photoURLs: [URL] {
        (photoUrl ?? "").split(separator: " ").compactMap { piece in
            guard let url = URL(string: String(piece)) else { return nil }
            return url.driveThumbnailURL ?? url
        }
    }

    enum CodingKeys: String, CodingKey {
        case datetime, time, foods, note, template, calories, items, edited
        case proteinG = "protein_g"
        case carbsG = "carbs_g"
        case fatG = "fat_g"
        case photoUrl = "photo_url"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        datetime = try c.decode(String.self, forKey: .datetime)
        time = try c.decode(String.self, forKey: .time)
        foods = try c.decode(String.self, forKey: .foods)
        note = try c.decode(String.self, forKey: .note)
        template = try c.decode(String.self, forKey: .template)
        calories = try c.decode(Double.self, forKey: .calories)
        proteinG = try c.decode(Double.self, forKey: .proteinG)
        carbsG = try c.decode(Double.self, forKey: .carbsG)
        fatG = try c.decode(Double.self, forKey: .fatG)
        photoUrl = try c.decodeIfPresent(String.self, forKey: .photoUrl)
        edited = try c.decodeIfPresent(Bool.self, forKey: .edited) ?? false
        items = try c.decode([MealItem].self, forKey: .items)
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

    // Not unique within a meal — "potato" and "boiled potato" are separate items
    // that both display as "batata". Anything iterating items keys on position
    // instead; this exists only to satisfy Identifiable.
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

// MARK: - Backend photo proxy URL conversion

private extension URL {
    /// Converts a Google Drive webViewLink
    /// (https://drive.google.com/file/d/FILE_ID/view) to a backend proxy URL
    /// that AsyncImage can render without Google Drive auth.
    var driveThumbnailURL: URL? {
        let components = pathComponents
        guard components.count >= 3,
              let dIndex = components.firstIndex(of: "d"),
              dIndex + 1 < components.count
        else { return nil }
        return Config.baseURL
            .appending(path: "meal-photo")
            .appending(path: components[dIndex + 1])
    }
}
