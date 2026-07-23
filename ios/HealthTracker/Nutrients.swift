//
//  Nutrients.swift
//  HealthTracker
//
//  The presentation catalogue for the micronutrients: the pt-PT label and display
//  order for each backend key, plus the mapping of a nutrient to the biological
//  SECTION it belongs in. The section is *derived from the live target's kinetics*
//  (`horizon` + whether it carries a ceiling), never hard-coded — so the screen's
//  structure always matches the backend's science and can't silently drift from it.
//  The backend owns the data (amounts, floors, ceilings, horizon); the app owns how
//  it reads to a Portuguese-speaking human. Keys must match the backend's
//  NUTRIENT_KEYS.
//

import Foundation

/// The three biological behaviours «Nutrientes» is organised around. A nutrient's
/// home section is decided by its target (see `NutrientCatalog.members`), so it tracks
/// the backend's kinetics rather than a second, drift-prone copy of the taxonomy.
enum NutrientSection: String, CaseIterable, Identifiable {
    case diarios        // daily floor — repõe todos os dias (consistency is the story)
    case reservas       // rolling floor — o corpo acumula (read the multi-day average)
    case dailyLimits    // dietary limits — sodium, added sugar, sat/trans fat (stay under)
    case safetyCeilings // toxicity ULs — vitamins/minerals with a reachable upper limit

    var id: String { rawValue }

    var title: String {
        switch self {
        case .diarios:  return "Diários"
        case .reservas: return "Reservas"
        case .dailyLimits: return "Limites diários"
        case .safetyCeilings: return "Tetos de segurança"
        }
    }

    /// The one-line teaching caption under the section title — the science, in a phrase.
    var caption: String {
        switch self {
        case .diarios:  return "Repõe todos os dias — o que conta é a consistência"
        case .reservas: return "O corpo acumula — olha para a média, não para um só dia"
        case .dailyLimits: return "O objetivo é ficar abaixo do máximo, todos os dias"
        case .safetyCeilings: return "Vitaminas e minerais com teto de toxicidade — raramente um risco real à mesa"
        }
    }

    var systemImage: String {
        switch self {
        case .diarios:  return "sun.max.fill"
        case .reservas: return "archivebox.fill"
        case .dailyLimits: return "exclamationmark.triangle.fill"
        case .safetyCeilings: return "shield.lefthalf.filled"
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
    /// Every catalogued nutrient in display order (vitamins, then minerals, then fats
    /// & fibre & the dietary limits). The section filters below preserve this order.
    static let all: [NutrientDef] = [
        // vitamins
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
        // minerals
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
        // fats, fibre & the dietary limits
        NutrientDef(key: "fiber_g", label: "Fibra"),
        NutrientDef(key: "omega3_g", label: "Ómega-3"),
        NutrientDef(key: "monounsaturated_fat_g", label: "Gordura monoinsaturada"),
        NutrientDef(key: "polyunsaturated_fat_g", label: "Gordura polinsaturada"),
        NutrientDef(key: "omega6_g", label: "Ómega-6"),
        NutrientDef(key: "sugar_g", label: "Açúcar total"),
        NutrientDef(key: "added_sugar_g", label: "Açúcar adicionado"),
        NutrientDef(key: "saturated_fat_g", label: "Gordura saturada"),
        NutrientDef(key: "trans_fat_g", label: "Gordura trans"),
        NutrientDef(key: "sodium_mg", label: "Sódio"),
        NutrientDef(key: "cholesterol_mg", label: "Colesterol"),
    ]

    /// Every catalogued nutrient, for lookups (e.g. the Today flags, the drill-down).
    static let byKey: [String: NutrientDef] = Dictionary(
        uniqueKeysWithValues: all.map { ($0.key, $0) })

    /// The nutrients to show in a section, derived from the LIVE targets so the screen
    /// always matches the backend's kinetics:
    ///  - `diarios`  — a floor to reach on a daily horizon (not body-banked);
    ///  - `reservas` — a floor to reach on a rolling horizon (body-banked);
    ///  - `dailyLimits` — a dietary limit to stay under (sodium, sugar, sat/trans fat);
    ///  - `safetyCeilings` — a toxicity upper limit on a reach/window nutrient.
    ///    This is cross-cutting: iron is a reserve with a UL, so it appears in both
    ///    `reservas` and `safetyCeilings`.
    static func members(_ section: NutrientSection,
                        targets: [String: Target]) -> [NutrientDef] {
        all.filter { def in
            guard let t = targets[def.key] else { return false }
            switch section {
            case .diarios:  return t.kind != Target.Kind.limit && !t.isRolling
            case .reservas: return t.kind != Target.Kind.limit && t.isRolling
            case .dailyLimits: return t.kind == Target.Kind.limit
            case .safetyCeilings: return t.upperLimit != nil
            }
        }
    }

    /// Catalogued nutrients eaten today that have NO target — the context fats and
    /// total sugar. Shown as amounts only, so nothing measured is silently dropped.
    static func context(consumed: [String: Double],
                        targets: [String: Target]) -> [NutrientDef] {
        all.filter { targets[$0.key] == nil && (consumed[$0.key] ?? 0) > 0 }
    }
}
