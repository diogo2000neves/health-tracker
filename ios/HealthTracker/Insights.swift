//
//  Insights.swift
//  HealthTracker
//
//  The shapes the coach returns: GET /insights/weekly (the Sunday review) and
//  GET /insights/next-meal (today's plates). Both are written by the local strong-model
//  job and served back from a cached sheet, so the app only ever reads them.
//
//  Decoded WITHOUT .convertFromSnakeCase (like the rest of the app): explicit
//  CodingKeys keep the mapping honest, and defensive decodeIfPresent means a report the
//  model phrased a little differently still decodes rather than blanking the screen.
//

import Foundation

// MARK: - GET /insights/weekly

struct WeeklyInsightsResponse: Decodable {
    /// "generated" once a report exists, "pending" until the first Sunday run lands
    /// (or if the Mac was offline). The view shows a calm placeholder while pending.
    let status: String
    let weekStart: String?
    let generatedAt: String?
    let windowStart: String?
    let windowEnd: String?
    let focusKey: String?
    /// The continuity fact — this week's focus nutrient measured against the one the
    /// LAST report set, so the coach can say "up 40% since I flagged it".
    let priorFocusDelta: ContinuityDelta?
    let coverageNote: String?
    let report: WeeklyReport?

    var isReady: Bool { status != "pending" && report != nil }

    enum CodingKeys: String, CodingKey {
        case status, report
        case weekStart = "week_start"
        case generatedAt = "generated_at"
        case windowStart = "window_start"
        case windowEnd = "window_end"
        case focusKey = "focus_key"
        case priorFocusDelta = "prior_focus_delta"
        case coverageNote = "coverage_note"
    }
}

/// The deterministic week-over-week move on the previous focus. `towardTarget` already
/// accounts for direction (more omega-3 is good; less sodium is good), so the UI can
/// colour it green/red without re-deriving the biology.
struct ContinuityDelta: Decodable {
    let key: String
    let pct: Double
    let direction: String          // up | down | flat
    let towardTarget: Bool

    enum CodingKeys: String, CodingKey {
        case key, pct, direction
        case towardTarget = "toward_target"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decodeIfPresent(String.self, forKey: .key) ?? ""
        pct = try c.decodeIfPresent(Double.self, forKey: .pct) ?? 0
        direction = try c.decodeIfPresent(String.self, forKey: .direction) ?? "flat"
        towardTarget = try c.decodeIfPresent(Bool.self, forKey: .towardTarget) ?? true
    }
}

/// The Layer-C narrative — one headline, the wins, the single focus, one swap. Every
/// field is one human sentence the model wrote from facts it was handed.
struct WeeklyReport: Decodable {
    let headline: String
    let wins: [Win]
    let focus: Focus
    let swap: FoodSwap?
    let continuity: String?
    let encouragement: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        headline = try c.decodeIfPresent(String.self, forKey: .headline) ?? ""
        wins = try c.decodeIfPresent([Win].self, forKey: .wins) ?? []
        focus = try c.decode(Focus.self, forKey: .focus)
        swap = try c.decodeIfPresent(FoodSwap.self, forKey: .swap)
        continuity = try c.decodeIfPresent(String.self, forKey: .continuity)
        encouragement = try c.decodeIfPresent(String.self, forKey: .encouragement)
    }

    enum CodingKeys: String, CodingKey {
        case headline, wins, focus, swap, continuity, encouragement
    }
}

struct Win: Decodable, Identifiable, Hashable {
    let title: String
    let detail: String
    var id: String { title + detail }
}

struct Focus: Decodable, Hashable {
    let key: String?
    let label: String
    let why: String
    let attribution: String?
    let severity: String?         // high | medium | low

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decodeIfPresent(String.self, forKey: .key)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        why = try c.decodeIfPresent(String.self, forKey: .why) ?? ""
        attribution = try c.decodeIfPresent(String.self, forKey: .attribution)
        severity = try c.decodeIfPresent(String.self, forKey: .severity)
    }

    enum CodingKeys: String, CodingKey { case key, label, why, attribution, severity }
}

struct FoodSwap: Decodable, Hashable {
    let from: String
    let to: String
    let why: String
}

// MARK: - GET /insights/next-meal (v2 — dynamic slot)

struct NextMealResponse: Decodable {
    let status: String
    let date: String?
    let generatedAt: String?
    let focusKey: String?
    /// The meal slot the AI determined is next, e.g. "pequeno-almoço", "almoço",
    /// "jantar", "lanche da manhã", "lanche da tarde".
    let nextSlot: String?
    let plates: [Plate]

    var isReady: Bool { status != "pending" && !plates.isEmpty }

    /// A localized label for the next slot, or nil if unknown.
    var slotLabel: String? {
        guard let s = nextSlot else { return nil }
        // Normalize and return the AI-written Portuguese label as-is.
        return s.prefix(1).uppercased() + s.dropFirst()
    }

    enum CodingKeys: String, CodingKey {
        case status, date, plates
        case generatedAt = "generated_at"
        case focusKey = "focus_key"
        case nextSlot = "next_slot"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "pending"
        date = try c.decodeIfPresent(String.self, forKey: .date)
        generatedAt = try c.decodeIfPresent(String.self, forKey: .generatedAt)
        focusKey = try c.decodeIfPresent(String.self, forKey: .focusKey)
        nextSlot = try c.decodeIfPresent(String.self, forKey: .nextSlot)
        plates = try c.decodeIfPresent([Plate].self, forKey: .plates) ?? []
    }
}

/// One suggested plate. `rank == 1 && recommended` is the pick; the other two are
/// alternatives. Portions are ranges the backend computed (never the model's guess).
struct Plate: Decodable, Identifiable, Hashable {
    let rank: Int
    let recommended: Bool
    let title: String
    let items: [PlateItem]
    let covers: [Cover]
    let calories: Double?
    let proteinG: Double?
    let why: String?

    var id: Int { rank }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rank = try c.decodeIfPresent(Int.self, forKey: .rank) ?? 1
        recommended = try c.decodeIfPresent(Bool.self, forKey: .recommended) ?? false
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        items = try c.decodeIfPresent([PlateItem].self, forKey: .items) ?? []
        covers = try c.decodeIfPresent([Cover].self, forKey: .covers) ?? []
        calories = try c.decodeIfPresent(Double.self, forKey: .calories)
        proteinG = try c.decodeIfPresent(Double.self, forKey: .proteinG)
        why = try c.decodeIfPresent(String.self, forKey: .why)
    }

    enum CodingKeys: String, CodingKey {
        case rank, recommended, title, items, covers, calories, why
        case proteinG = "protein_g"
    }
}

struct PlateItem: Decodable, Identifiable, Hashable {
    let food: String
    let gramsLow: Int
    let gramsHigh: Int
    let isNew: Bool

    var id: String { food }

    /// "120–150 g" — the range that meaningfully closes the gap.
    var portionText: String {
        gramsLow == gramsHigh ? "\(gramsLow) g" : "\(gramsLow)–\(gramsHigh) g"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        food = try c.decodeIfPresent(String.self, forKey: .food) ?? ""
        gramsLow = try c.decodeIfPresent(Int.self, forKey: .gramsLow) ?? 0
        gramsHigh = try c.decodeIfPresent(Int.self, forKey: .gramsHigh) ?? gramsLow
        isNew = try c.decodeIfPresent(Bool.self, forKey: .isNew) ?? false
    }

    enum CodingKeys: String, CodingKey {
        case food
        case gramsLow = "grams_low"
        case gramsHigh = "grams_high"
        case isNew = "new"
    }
}

/// What a plate fixes — the nutrient it closes, with a short human note ("fecha a
/// semana"). Drives the little tags on a plate card.
struct Cover: Decodable, Identifiable, Hashable {
    let key: String?
    let label: String
    let note: String?
    var id: String { (key ?? "") + label }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decodeIfPresent(String.self, forKey: .key)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        note = try c.decodeIfPresent(String.self, forKey: .note)
    }

    enum CodingKeys: String, CodingKey { case key, label, note }
}
