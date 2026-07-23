//
//  SampleData.swift
//  HealthTracker
//
//  Realistic sample payloads for SwiftUI previews and for verifying the UI without
//  a deployed backend (APIClient uses these when launched with USE_SAMPLE_DATA=1).
//  Built as JSON and decoded through the real decoders, so the sample also exercises
//  the exact decoding path production uses. `consumed` is SUMMED from the sample
//  meals' items, so the Nutrients bars and the food drill-down always agree — the
//  same guarantee the backend gives with real data.
//
//  DEBUG-only: never compiled into a release build.
//

#if DEBUG
import Foundation

enum SampleData {
    static let today: TodayResponse = decode(TodayResponse.self, from: todayJSON())
    static let daily: DailyResponse = decode(DailyResponse.self, from: dailyJSON())
    static let nutrients: NutrientInfoResponse = decode(NutrientInfoResponse.self,
                                                        from: nutrientsJSON())
    static let weeklyInsights: WeeklyInsightsResponse = decode(WeeklyInsightsResponse.self,
                                                               from: weeklyJSON())
    static let nextMeal: NextMealResponse = decode(NextMealResponse.self,
                                                   from: nextMealJSON())

    // MARK: - /nutrients (a few populated examples; the rest render "em breve")

    private static func nutrientsJSON() -> [String: Any] {
        func src(_ food: String, _ amount: Double, _ unit: String, _ note: String = "") -> [String: Any] {
            var d: [String: Any] = ["food": food, "amount": amount, "unit": unit, "per": "100 g"]
            if !note.isEmpty { d["note"] = note }
            return d
        }
        return [
            "version": 2,
            "nutrients": [
                "iron_mg": [
                    "summary": "Mineral vestigial que transporta oxigénio no sangue (hemoglobina) e nos músculos (mioglobina) e alimenta a produção de energia.",
                    "roles": ["Transporte de oxigénio no sangue e músculos", "Produção de energia na mitocôndria", "Síntese de ADN e de neurotransmissores"],
                    "goal_relevance": "O homem não tem forma de eliminar ferro a mais — a acumulação é um risco maior do que a falta. Ferro baixo faz os treinos parecerem mais duros. Não suplementes sem análises.",
                    "optimal_range": "8–10 mg/dia (só da comida)",
                    "upper_limit": "45 mg/dia",
                    "food_sources": [src("Fígado de vaca", 6.5, "mg", "heme, 15–35% absorvido"), src("Ostras cozidas", 8, "mg", "heme"), src("Carne de vaca magra", 2.6, "mg", "heme"), src("Lentilhas cozidas", 3.3, "mg", "não-heme, 2–10%"), src("Espinafres cozidos", 3.6, "mg", "só ~2%")],
                    "deficiency": "Cansaço, falta de ar no esforço, mãos frias, pernas inquietas e recuperação fraca; avançada: anemia.",
                    "excess": "O ferro livre gera radicais e deposita-se no fígado, pâncreas e coração.",
                    "tips": ["A vitamina C aumenta a absorção do ferro vegetal em 200–300%", "Café e chá à refeição reduzem a absorção em 50–70%", "O cálcio inibe a absorção — separa-o"],
                    "fact": "Um homem adulto tem cerca de 4 g de ferro, a maioria nos glóbulos vermelhos.",
                    "references": ["National Academies DRI — Iron"],
                ],
                "magnesium_mg": [
                    "summary": "Cofator obrigatório de mais de 600 reações. Estabiliza o ATP, acalma o sistema nervoso e relaxa o músculo.",
                    "roles": ["Produção de energia (Mg-ATP)", "Relaxamento muscular", "Bloqueio do recetor NMDA — calma e sono", "Saúde óssea"],
                    "goal_relevance": "O mineral que se 'queima' mais depressa num homem ativo: treino, stress e cafeína aceleram o consumo. A falta subclínica confunde-se com ansiedade ou overtraining.",
                    "optimal_range": "400–600 mg/dia",
                    "upper_limit": "350 mg/dia (só de suplementos)",
                    "food_sources": [src("Sementes de abóbora", 590, "mg"), src("Sementes de cânhamo", 700, "mg"), src("Chocolate preto (85%+)", 230, "mg"), src("Espinafres cozidos", 87, "mg"), src("Feijão preto cozido", 70, "mg")],
                    "deficiency": "Pálpebra a tremer, cãibras, dores de cabeça, ansiedade, insónia e sensibilidade à cafeína.",
                    "excess": "Quase impossível pela comida; doses altas de suplementos causam diarreia.",
                    "tips": ["A B6 aumenta a captação e, juntos, produzem GABA (calma)", "O glicinato é bom para o sono; o malato para a manhã"],
                    "fact": "Cerca de 60% do magnésio do corpo está guardado nos ossos.",
                    "references": ["National Academies DRI — Magnesium"],
                ],
                "zinc_mg": [
                    "summary": "Mineral vestigial cofator de mais de 300 enzimas. É o 'mineral masculino' — central para a testosterona e a imunidade.",
                    "roles": ["Síntese de testosterona", "Cofator de 300+ enzimas e 'dedos de zinco'", "Maturação das células imunitárias", "Cicatrização e paladar"],
                    "goal_relevance": "Perde-se 1–3 mg por ejaculação e mais no suor — a falta é comum em homens jovens ativos. Baixa de zinco = menos testosterona livre e mais infeções.",
                    "optimal_range": "15–30 mg/dia",
                    "upper_limit": "40 mg/dia",
                    "food_sources": [src("Ostras", 30, "mg", "a maior densidade de zinco"), src("Carne de vaca (acém)", 8.5, "mg"), src("Sementes de abóbora", 7.8, "mg"), src("Peito de frango", 1.0, "mg")],
                    "deficiency": "Testosterona baixa, menos libido, infeções, acne e perda de paladar/olfato.",
                    "excess": "Náuseas em jejum; doses altas crónicas provocam falta de cobre.",
                    "tips": ["Nunca em jejum — toma a meio de uma refeição", "Se tomas 30–50 mg/dia, junta 2–3 mg de cobre"],
                    "fact": "Mantém uma razão zinco:cobre de ~10:1 a 15:1 para a defesa antioxidante.",
                    "references": ["National Academies DRI — Zinc"],
                ],
            ],
        ]
    }

    // MARK: - /today

    private static func todayJSON() -> [String: Any] {
        let meals = sampleMeals()
        let consumed = sumConsumed(from: meals)
        return [
            "date": isoDay(0),
            "meal_count": meals.count,
            "consumed": consumed,
            "targets": sampleTargets(),
            "basis": [
                "tdee_kcal": 2400, "calorie_target_kcal": 2100,
                "weight_kg": 70.2, "lean_mass_kg": 56.5,
                "protein_g_per_kg": 2.0, "calorie_deficit_pct": 12.5, "goal": "recomp",
            ],
            "meals": meals,
            "history": sampleHistory(today: consumed),
        ]
    }

    /// Seven completed days of intake, tuned so each biological lens has a clear story
    /// to tell offline: Vitamin D low today but a healthy 7-day average (don't panic),
    /// Vitamin C consistent (6/7 days on target), magnésio inconsistent (3/7), and iron
    /// whose reserves sit right up against the 45 mg toxicity ceiling. Every other
    /// nutrient is a realistic full-day scaling of today's partial intake.
    private static func sampleHistory(today: [String: Double]) -> [[String: Any]] {
        let vitD: [Double]      = [18, 20, 16, 22, 14, 19, 17]        // reserves fine
        let vitC: [Double]      = [140, 110, 70, 160, 180, 130, 120]  // consistent (6/7 ≥ 90)
        let magnesium: [Double] = [420, 300, 260, 450, 380, 500, 280] // inconsistent (3/7 ≥ 400)
        let iron: [Double]      = [42, 38, 45, 30, 44, 40, 41]        // avg ~40, near the 45 UL
        var days: [[String: Any]] = []
        for i in 0..<7 {
            let ago = 7 - i                                            // oldest (7) -> yesterday (1)
            let factor = 1.3 + wave(i, 4.4) * 0.25                     // a fuller day than today so far
            var consumed = today.mapValues { round1($0 * factor) }
            consumed["vitamin_d_ug"] = vitD[i]
            consumed["vitamin_c_mg"] = vitC[i]
            consumed["magnesium_mg"] = magnesium[i]
            consumed["iron_mg"] = iron[i]
            days.append(["date": isoDay(-ago), "consumed": consumed])
        }
        return days
    }

    /// Three meals of a day in progress (mid-afternoon), each ingredient carrying a
    /// realistic nutrient map so the drill-down has something to show.
    private static func sampleMeals() -> [[String: Any]] {
        [
            meal("08:20", "Aveia, leite, banana e manteiga de amendoim", note: "", items: [
                item("aveia", 60, 228, 8, 40, 4,
                     ["fiber_g": 6, "magnesium_mg": 84, "iron_mg": 2.4, "zinc_mg": 1.5,
                      "phosphorus_mg": 210, "potassium_mg": 210, "vitamin_b1_mg": 0.3]),
                item("leite meio-gordo", 200, 100, 7, 10, 3.4,
                     ["calcium_mg": 240, "vitamin_b12_ug": 0.9, "vitamin_b2_mg": 0.4,
                      "potassium_mg": 300, "vitamin_d_ug": 1.1, "saturated_fat_g": 2.1,
                      "sugar_g": 10, "iodine_ug": 30, "phosphorus_mg": 190]),
                item("banana", 120, 107, 1.3, 27, 0.4,
                     ["fiber_g": 3.1, "potassium_mg": 430, "vitamin_b6_mg": 0.4,
                      "vitamin_c_mg": 10, "magnesium_mg": 32, "sugar_g": 14]),
                item("manteiga de amendoim", 20, 118, 5, 4, 10,
                     ["fiber_g": 1.7, "vitamin_e_mg": 1.8, "magnesium_mg": 34,
                      "niacin_mg": 2.7, "vitamin_b3_mg": 2.7, "monounsaturated_fat_g": 5,
                      "sodium_mg": 85, "potassium_mg": 100]),
            ]),
            meal("13:15", "Frango grelhado, arroz e brócolos", note: "azeite q.b.", items: [
                item("peito de frango grelhado", 180, 297, 56, 0, 7,
                     ["sodium_mg": 130, "potassium_mg": 440, "phosphorus_mg": 360,
                      "zinc_mg": 1.8, "vitamin_b6_mg": 1.1, "vitamin_b3_mg": 24,
                      "selenium_ug": 44, "cholesterol_mg": 145, "saturated_fat_g": 2]),
                item("arroz branco cozido", 200, 260, 5, 56, 0.6,
                     ["fiber_g": 1.2, "magnesium_mg": 24, "iron_mg": 1.4,
                      "folate_ug": 60, "potassium_mg": 55, "selenium_ug": 15]),
                item("brócolos", 150, 51, 4.2, 10, 0.6,
                     ["fiber_g": 3.9, "vitamin_c_mg": 132, "vitamin_k_ug": 155,
                      "folate_ug": 96, "potassium_mg": 468, "calcium_mg": 71,
                      "vitamin_a_ug": 46]),
                item("azeite", 10, 88, 0, 0, 10,
                     ["vitamin_e_mg": 1.9, "vitamin_k_ug": 6,
                      "monounsaturated_fat_g": 7.3, "saturated_fat_g": 1.4]),
            ]),
            meal("16:40", "Iogurte grego com mirtilos e amêndoas", note: "", items: [
                item("iogurte grego", 170, 100, 17, 6, 0.7,
                     ["calcium_mg": 190, "vitamin_b12_ug": 1.3, "vitamin_b2_mg": 0.4,
                      "phosphorus_mg": 230, "potassium_mg": 240, "sugar_g": 6,
                      "iodine_ug": 50, "zinc_mg": 1.1]),
                item("mirtilos", 80, 46, 0.6, 12, 0.3,
                     ["fiber_g": 1.9, "vitamin_c_mg": 8, "vitamin_k_ug": 15,
                      "manganese_mg": 0.3, "sugar_g": 8]),
                item("amêndoas", 25, 145, 5.3, 5.4, 12.5,
                     ["fiber_g": 3.1, "vitamin_e_mg": 6.5, "magnesium_mg": 68,
                      "calcium_mg": 66, "manganese_mg": 0.5, "copper_mg": 0.3,
                      "monounsaturated_fat_g": 8, "potassium_mg": 180]),
            ]),
        ]
    }

    /// Sum every item's macros and nutrients into the `consumed` map — the same
    /// aggregation the backend does server-side, kept here so the sample is honest.
    private static func sumConsumed(from meals: [[String: Any]]) -> [String: Double] {
        var totals: [String: Double] = [:]
        let macroKeys = ["calories", "protein_g", "carbs_g", "fat_g"]
        for meal in meals {
            for key in macroKeys {
                totals[key, default: 0] += (meal[key] as? Double) ?? 0
            }
            for item in (meal["items"] as? [[String: Any]] ?? []) {
                for (k, v) in (item["nutrients"] as? [String: Double] ?? [:]) {
                    totals[k, default: 0] += v
                }
            }
        }
        return totals.mapValues { ($0 * 10).rounded() / 10 }
    }

    private static func sampleTargets() -> [String: Any] {
        var t: [String: Any] = [
            // measured macros (TDEE 2400, weight 70, recomp)
            "calories": target("window", floor: 1920, ceiling: 2280, unit: "kcal", source: "measured"),
            "protein_g": target("reach", floor: 140, unit: "g", source: "measured"),
            "fat_g": target("reach", floor: 56, unit: "g", source: "measured"),
            "carbs_g": target("window", floor: 233, ceiling: 285, unit: "g", source: "measured"),
            "fiber_g": target("reach", floor: 29, unit: "g", source: "measured"),
            "added_sugar_g": target("limit", ceiling: 52, unit: "g", source: "measured"),
            "saturated_fat_g": target("limit", ceiling: 23, unit: "g", source: "measured"),
        ]
        // rda micros (adult male 19-50) with their kinetics, mirroring the backend's
        // _MICRO_TARGETS + _NUTRIENT_KINETICS: (key, floor, unit, horizon, UL?).
        let reach: [(String, Double, String, String, Double?)] = [
            ("vitamin_a_ug", 900, "ug", "rolling", 3000), ("vitamin_c_mg", 90, "mg", "daily", nil),
            ("vitamin_d_ug", 15, "ug", "rolling", 100), ("vitamin_e_mg", 15, "mg", "rolling", 1000),
            ("vitamin_k_ug", 120, "ug", "rolling", nil), ("vitamin_b1_mg", 1.2, "mg", "daily", nil),
            ("vitamin_b2_mg", 1.3, "mg", "daily", nil), ("vitamin_b3_mg", 16, "mg", "daily", nil),
            ("vitamin_b5_mg", 5, "mg", "daily", nil), ("vitamin_b6_mg", 1.3, "mg", "daily", nil),
            ("vitamin_b12_ug", 2.4, "ug", "rolling", nil), ("folate_ug", 400, "ug", "rolling", nil),
            ("biotin_ug", 30, "ug", "daily", nil), ("choline_mg", 550, "mg", "daily", nil),
            ("calcium_mg", 1000, "mg", "rolling", 2500), ("iron_mg", 8, "mg", "rolling", 45),
            ("magnesium_mg", 400, "mg", "daily", nil), ("zinc_mg", 11, "mg", "daily", 40),
            ("potassium_mg", 3400, "mg", "daily", nil), ("phosphorus_mg", 700, "mg", "rolling", 4000),
            ("copper_mg", 0.9, "mg", "rolling", 10), ("manganese_mg", 2.3, "mg", "rolling", 11),
            ("selenium_ug", 55, "ug", "rolling", 400), ("iodine_ug", 150, "ug", "rolling", 1100),
            ("chloride_mg", 2300, "mg", "daily", nil), ("omega3_g", 1.6, "g", "rolling", nil),
        ]
        for (k, f, u, h, ul) in reach {
            t[k] = target("reach", floor: f, ceiling: ul, unit: u, source: "rda", horizon: h)
        }
        t["sodium_mg"] = target("limit", ceiling: 2300, unit: "mg", source: "rda")
        t["trans_fat_g"] = target("limit", ceiling: 2, unit: "g", source: "rda")
        // cholesterol_mg has no target: the fixed 300 mg/day cap is no longer
        // evidence-based (see backend/ingest/main.py's _MICRO_TARGETS comment), so
        // it shows in the Context section (amount only) rather than as a limit.
        return t
    }

    // MARK: - /daily (90 days of plausible recomposition history)

    private static func dailyJSON() -> [String: Any] {
        var days: [[String: Any]] = []
        let n = 90
        for i in 0..<n {
            let t = Double(i) / Double(n - 1)                 // 0 (oldest) -> 1 (yesterday)
            let ago = n - i                                   // yesterday = 1
            let weight = 71.6 - 1.5 * t + wave(i, 0.9) * 0.35
            let bodyFat = 21.4 - 2.8 * t + wave(i, 2.1) * 0.4
            let muscle = 53.0 + 1.1 * t + wave(i, 3.7) * 0.15
            let lean = weight * (1 - bodyFat / 100)
            let calsOut = 2300 + wave(i, 1.3) * 230 + (wave(i, 5.0) > 0.4 ? 250 : 0)
            // protein mostly on target, with a clean streak over the last 6 days
            let protein = ago <= 6 ? 148 + wave(i, 4.0) * 8 : 132 + wave(i, 2.7) * 26
            let calsIn = 2050 + wave(i, 0.7) * 260
            let fiber = 27 + wave(i, 1.9) * 8
            var day: [String: Any] = [
                "date": isoDay(-ago),
                "nutrition": [
                    "total_cals_in": round(calsIn),
                    "total_protein_g": round(protein),
                    "total_carbs_g": round(210 + wave(i, 2.3) * 40),
                    "total_fat_g": round(62 + wave(i, 1.1) * 12),
                    "total_fiber_g": round(fiber),
                    "energy_balance_kcal": round(calsIn - calsOut),
                ],
                "activity": [
                    "total_cals_out": round(calsOut),
                    "steps": Int(7200 + wave(i, 3.1) * 3500),
                    "total_active_mins": Int(45 + wave(i, 2.0) * 30),
                ],
                "sleep": [
                    "sleep_mins": Int(432 + wave(i, 1.7) * 46),
                    "sleep_efficiency_pct": round(90 + wave(i, 2.9) * 4),
                ],
                "recovery": [
                    "resting_hr_bpm": Int(54 + wave(i, 2.4) * 3),
                    "hrv_ms": round(72 + wave(i, 3.3) * 12),
                ],
            ]
            // a weigh-in on most mornings (a few skipped, as in real life)
            if wave(i, 6.0) > -0.75 {
                day["body"] = [
                    "weight_kg": round2(weight),
                    "body_fat_pct": round1(bodyFat),
                    "muscle_mass_kg": round1(muscle),
                    "lean_mass_kg": round2(lean),
                    "visceral_fat": round1(6.5 - 1.2 * t),
                ]
            }
            days.append(day)
        }
        return [
            "from": isoDay(-n), "to": isoDay(-1),
            "count": days.count,
            "blocks": ["self_report", "sleep", "recovery", "activity", "nutrition", "body"],
            "days": days,
        ]
    }

    // MARK: - builders & helpers

    private static func meal(_ time: String, _ foods: String, note: String,
                             items: [[String: Any]]) -> [String: Any] {
        let cal = items.reduce(0.0) { $0 + (($1["calories"] as? Double) ?? 0) }
        let p = items.reduce(0.0) { $0 + (($1["protein_g"] as? Double) ?? 0) }
        let c = items.reduce(0.0) { $0 + (($1["carbs_g"] as? Double) ?? 0) }
        let f = items.reduce(0.0) { $0 + (($1["fat_g"] as? Double) ?? 0) }
        return [
            "datetime": "\(isoDay(0))T\(time):00+01:00", "time": time,
            "foods": foods, "note": note, "template": "",
            "calories": round(cal), "protein_g": round1(p), "carbs_g": round1(c),
            "fat_g": round1(f), "items": items,
        ]
    }

    private static func item(_ name: String, _ portion: Double, _ cal: Double,
                             _ p: Double, _ c: Double, _ f: Double,
                             _ nutrients: [String: Double]) -> [String: Any] {
        ["name": name, "portion_g": portion, "calories": cal,
         "protein_g": p, "carbs_g": c, "fat_g": f, "nutrients": nutrients]
    }

    private static func target(_ kind: String, floor: Double? = nil, ceiling: Double? = nil,
                               unit: String, source: String,
                               horizon: String = "daily") -> [String: Any] {
        var t: [String: Any] = ["kind": kind, "unit": unit, "source": source, "horizon": horizon]
        if let floor { t["floor"] = floor }
        if let ceiling { t["ceiling"] = ceiling }
        return t
    }

    /// Deterministic pseudo-noise in roughly [-1, 1], so screenshots are stable.
    private static func wave(_ i: Int, _ salt: Double) -> Double {
        sin(Double(i) * 0.9 + salt) * 0.6 + sin(Double(i) * 0.37 + salt * 2) * 0.4
    }

    private static func isoDay(_ offset: Int) -> String {
        let day = Calendar.current.date(byAdding: .day, value: offset, to: Date())!
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: day)
    }

    private static func round(_ x: Double) -> Double { x.rounded() }
    private static func round1(_ x: Double) -> Double { (x * 10).rounded() / 10 }
    private static func round2(_ x: Double) -> Double { (x * 100).rounded() / 100 }

    // MARK: - /insights/weekly (a full, realistic Sunday review)

    private static func weeklyJSON() -> [String: Any] {
        [
            "status": "generated",
            "week_start": "2026-07-19",
            "generated_at": "2026-07-19T09:00:00",
            "window_start": "2026-07-12",
            "window_end": "2026-07-18",
            "focus_key": "saturated_fat_g",
            "prior_focus_delta": ["key": "omega3_g", "prev": 0.9, "now": 1.3,
                                  "pct": 44, "direction": "up", "toward_target": true],
            "coverage_note": "Boa cobertura de dados esta semana.",
            "report": [
                "headline": "Semana forte: proteína cravada e ómega-3 a subir — só a "
                    + "gordura saturada é que pede atenção.",
                "wins": [
                    ["title": "Proteína no ponto",
                     "detail": "Bateste a meta em 6 dos 7 dias — é isto que protege o "
                        + "músculo enquanto perdes gordura."],
                    ["title": "Ómega-3 a subir",
                     "detail": "As sardinhas de terça e sexta fizeram a diferença."],
                ],
                "focus": [
                    "key": "saturated_fat_g", "label": "Gordura saturada",
                    "why": "Ficaste cerca de 28% acima do teto em 5 dos 7 dias.",
                    "attribution": "68% vem do chouriço nas refeições de sábado e domingo.",
                    "severity": "high",
                ],
                "swap": [
                    "from": "chouriço", "to": "peito de peru fumado",
                    "why": "Mantém o salgado de que gostas com cerca de 1/5 da gordura "
                        + "saturada.",
                ],
                "continuity": "O ómega-3 subiu 44% desde domingo passado — continua com "
                    + "o peixe 2x/semana.",
                "encouragement": "Uma troca só e a semana fica redonda. Vais lá "
                    + "facilmente.",
            ],
        ]
    }

    // MARK: - /insights/next-meal (three ranked plates)

    private static func nextMealJSON() -> [String: Any] {
        func item(_ food: String, _ lo: Int, _ hi: Int, new: Bool = false) -> [String: Any] {
            ["food": food, "grams_low": lo, "grams_high": hi, "new": new]
        }
        func cover(_ key: String, _ label: String, _ note: String) -> [String: Any] {
            ["key": key, "label": label, "note": note]
        }
        return [
            "status": "generated",
            "date": "2026-07-19",
            "generated_at": "2026-07-19T17:32:00",
            "focus_key": "saturated_fat_g",
            "plates": [
                [
                    "rank": 1, "recommended": true,
                    "title": "Salmão no forno com brócolos e arroz",
                    "items": [item("salmão", 140, 170), item("brócolos", 120, 150),
                              item("arroz", 60, 90)],
                    "covers": [cover("omega3_g", "Ómega-3", "fecha a semana"),
                               cover("protein_g", "Proteína", "+38 g")],
                    "calories": 640, "protein_g": 42,
                    "why": "Fecha o ómega-3 da semana e a proteína de hoje, com comida "
                        + "que já comes.",
                ],
                [
                    "rank": 2, "recommended": false,
                    "title": "Omelete de 3 ovos com espinafres e queijo fresco",
                    "items": [item("ovos", 150, 180), item("espinafres", 80, 120),
                              item("queijo fresco", 60, 80)],
                    "covers": [cover("protein_g", "Proteína", "+30 g"),
                               cover("vitamin_d_ug", "Vitamina D", "")],
                    "calories": 480, "protein_g": 33,
                    "why": "Rápida e rica em proteína; os espinafres puxam pelo ferro.",
                ],
                [
                    "rank": 3, "recommended": false,
                    "title": "Bowl de grão com atum e legumes",
                    "items": [item("grão", 120, 150), item("atum", 80, 100),
                              item("edamame", 60, 90, new: true), item("azeite", 10, 15)],
                    "covers": [cover("fiber_g", "Fibra", "+9 g"),
                               cover("protein_g", "Proteína", "+28 g")],
                    "calories": 560, "protein_g": 31,
                    "why": "Fibra e proteína numa tigela; o grão mantém-te saciado.",
                ],
            ],
        ]
    }

    private static func decode<T: Decodable>(_ type: T.Type, from dict: [String: Any]) -> T {
        let data = try! JSONSerialization.data(withJSONObject: dict)
        return try! JSONDecoder().decode(T.self, from: data)
    }
}
#endif
