//
//  Nutrients.swift
//  HealthTracker
//
//  The presentation catalogue for the 37 micronutrients: the pt-PT label, the
//  group and the display order for each backend key. The backend owns the data
//  (amounts + targets, in English snake_case keys); the app owns how it reads to a
//  Portuguese-speaking human. Keys here are the single source of truth that must
//  match the backend's NUTRIENT_KEYS.
//

import Foundation

/// The four groups the Nutrients screen is organised into, in display order.
enum NutrientGroup: String, CaseIterable, Identifiable {
    case watch       // limits — surfaced first ("show when over", not only under)
    case vitamins
    case minerals
    case fatsFiber

    var id: String { rawValue }

    var title: String {
        switch self {
        case .watch:     return "A vigiar"
        case .vitamins:  return "Vitaminas"
        case .minerals:  return "Minerais"
        case .fatsFiber: return "Gorduras & fibra"
        }
    }

    var systemImage: String {
        switch self {
        case .watch:     return "exclamationmark.triangle.fill"
        case .vitamins:  return "pills.fill"
        case .minerals:  return "bolt.fill"
        case .fatsFiber: return "drop.fill"
        }
    }
}

struct NutrientDef: Identifiable, Hashable {
    let key: String       // backend key, e.g. "vitamin_b12_ug"
    let label: String     // pt-PT display name
    var id: String { key }

    /// Display unit derived from the key suffix (µg reads nicer than the raw "ug").
    var unit: String {
        if key.hasSuffix("_ug") { return "µg" }
        if key.hasSuffix("_mg") { return "mg" }
        return "g"
    }

    /// A human amount string, e.g. "1.3 mg", "150 µg", "29 g". Small values keep a
    /// decimal so 0.9 mg doesn't collapse to "1 mg"; large ones are whole.
    func amount(_ value: Double) -> String {
        let decimals: Int
        if unit == "g" { decimals = value >= 100 ? 0 : 1 }
        else { decimals = value >= 10 ? 0 : 1 }
        return value.formatted(.number.precision(.fractionLength(decimals))) + " " + unit
    }
}

enum NutrientCatalog {
    static let groups: [NutrientGroup: [NutrientDef]] = [
        .watch: [
            NutrientDef(key: "sodium_mg", label: "Sódio"),
            NutrientDef(key: "added_sugar_g", label: "Açúcar adicionado"),
            NutrientDef(key: "saturated_fat_g", label: "Gordura saturada"),
            NutrientDef(key: "trans_fat_g", label: "Gordura trans"),
            NutrientDef(key: "cholesterol_mg", label: "Colesterol"),
        ],
        .vitamins: [
            NutrientDef(key: "vitamin_a_ug", label: "Vitamina A"),
            NutrientDef(key: "vitamin_c_mg", label: "Vitamina C"),
            NutrientDef(key: "vitamin_d_ug", label: "Vitamina D"),
            NutrientDef(key: "vitamin_e_mg", label: "Vitamina E"),
            NutrientDef(key: "vitamin_k_ug", label: "Vitamina K"),
            NutrientDef(key: "vitamin_b1_mg", label: "B1 · Tiamina"),
            NutrientDef(key: "vitamin_b2_mg", label: "B2 · Riboflavina"),
            NutrientDef(key: "vitamin_b3_mg", label: "B3 · Niacina"),
            NutrientDef(key: "vitamin_b5_mg", label: "B5 · Ác. pantoténico"),
            NutrientDef(key: "vitamin_b6_mg", label: "Vitamina B6"),
            NutrientDef(key: "vitamin_b12_ug", label: "Vitamina B12"),
            NutrientDef(key: "folate_ug", label: "Folato"),
            NutrientDef(key: "biotin_ug", label: "Biotina"),
        ],
        .minerals: [
            NutrientDef(key: "calcium_mg", label: "Cálcio"),
            NutrientDef(key: "iron_mg", label: "Ferro"),
            NutrientDef(key: "magnesium_mg", label: "Magnésio"),
            NutrientDef(key: "zinc_mg", label: "Zinco"),
            NutrientDef(key: "potassium_mg", label: "Potássio"),
            NutrientDef(key: "phosphorus_mg", label: "Fósforo"),
            NutrientDef(key: "copper_mg", label: "Cobre"),
            NutrientDef(key: "manganese_mg", label: "Manganês"),
            NutrientDef(key: "selenium_ug", label: "Selénio"),
            NutrientDef(key: "iodine_ug", label: "Iodo"),
            NutrientDef(key: "choline_mg", label: "Colina"),
            NutrientDef(key: "chloride_mg", label: "Cloreto"),
        ],
        .fatsFiber: [
            NutrientDef(key: "fiber_g", label: "Fibra"),
            NutrientDef(key: "omega3_g", label: "Ómega-3"),
            NutrientDef(key: "monounsaturated_fat_g", label: "Gordura monoinsaturada"),
            NutrientDef(key: "polyunsaturated_fat_g", label: "Gordura polinsaturada"),
            NutrientDef(key: "omega6_g", label: "Ómega-6"),
            NutrientDef(key: "sugar_g", label: "Açúcar total"),
        ],
    ]

    static func defs(_ group: NutrientGroup) -> [NutrientDef] { groups[group] ?? [] }

    /// Every catalogued nutrient, for lookups (e.g. the drill-down label).
    static let byKey: [String: NutrientDef] = Dictionary(
        uniqueKeysWithValues: groups.values.flatMap { $0 }.map { ($0.key, $0) })

    static func label(for key: String) -> String { byKey[key]?.label ?? key }
}
