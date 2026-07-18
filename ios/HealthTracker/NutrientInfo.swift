//
//  NutrientInfo.swift
//  HealthTracker
//
//  The per-nutrient reference knowledge base (GET /nutrients) and a small store
//  that caches it. This is static educational content — the same for everyone and
//  rarely changing — so it's fetched once and kept in memory. Every field is
//  optional: a nutrient that hasn't been written up yet simply renders nothing
//  ("em breve"), and the deep-info screen shows only the sections that have content.
//

import Foundation

struct NutrientInfoResponse: Decodable {
    let version: Int?
    let nutrients: [String: NutrientInfo]
}

struct NutrientInfo: Decodable, Hashable {
    var summary: String?
    var roles: [String]?
    var goalRelevance: String?
    var optimalRange: String?
    var upperLimit: String?
    var foodSources: [FoodSource]?
    var deficiency: String?
    var excess: String?
    var tips: [String]?
    var fact: String?
    var sections: [InfoSection]?
    var references: [String]?

    enum CodingKeys: String, CodingKey {
        case summary, roles, deficiency, excess, tips, fact, sections, references
        case goalRelevance = "goal_relevance"
        case optimalRange = "optimal_range"
        case upperLimit = "upper_limit"
        case foodSources = "food_sources"
    }

    /// Whether there's anything worth showing. An all-blank template row (present so
    /// the table has a slot for every nutrient) reads as "not written up yet".
    var hasContent: Bool {
        [summary, goalRelevance, deficiency, excess, fact].contains { !($0 ?? "").isEmpty }
            || [roles, tips].contains { !($0 ?? []).isEmpty }
            || !(foodSources ?? []).isEmpty
            || !(sections ?? []).isEmpty
    }
}

/// A food rich in a nutrient, with the amount per serving — structured so a future
/// meal-recommendation feature can rank foods by nutrient density.
struct FoodSource: Decodable, Hashable, Identifiable {
    let food: String
    let amount: Double?
    let unit: String?
    let per: String?
    let note: String?

    var id: String { food }

    /// "6.5 mg / 100 g" — the amount rendered on the chip.
    var amountText: String {
        guard let amount, let unit else { return "" }
        let decimals = amount >= 100 ? 0 : (amount < 10 ? 1 : 0)
        let value = amount.formatted(.number.precision(.fractionLength(decimals)))
        return "\(value) \(unit)" + (per.map { " / \($0)" } ?? "")
    }
}

struct InfoSection: Decodable, Hashable, Identifiable {
    let title: String
    let body: String
    var id: String { title }
}

@MainActor
@Observable
final class InfoStore {
    private(set) var byKey: [String: NutrientInfo] = [:]
    private(set) var loaded = false
    private var loading = false

    /// Fetch once and cache. Non-critical: on failure it stays empty (nutrients show
    /// "em breve") and a later open retries.
    func loadIfNeeded() async {
        if loaded || loading { return }
        loading = true
        defer { loading = false }
        do {
            byKey = try await APIClient.shared.nutrients().nutrients
            loaded = true
        } catch {
            // silent — deep info is a nice-to-have, never blocks the nutrients screen
        }
    }

    /// The write-up for a nutrient, or nil when there's nothing to show yet.
    func info(for key: String) -> NutrientInfo? {
        guard let info = byKey[key], info.hasContent else { return nil }
        return info
    }
}
