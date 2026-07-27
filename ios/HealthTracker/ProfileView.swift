//
//  ProfileView.swift
//  HealthTracker
//
//  Perfil & objetivos — the goal, the body inputs the targets are derived from, and
//  the derived targets themselves, shown read-only and honestly (every number names
//  where it came from). Deep edits happen in the `targets` tab of the sheet, which
//  this screen points to rather than duplicating.
//

import SwiftUI

struct ProfileView: View {
    let store: TodayStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if let r = store.response {
                    goalSection(r)
                    bodySection(r.basis)
                    targetsSection(r)
                    measuringSection(r.caps)
                    Section {
                        Text("Estes objetivos são calculados a partir dos teus próprios dados e atualizam-se sozinhos. Para ajustes finos, edita o separador `targets` na folha de cálculo; para mudar o que a app acompanha, o separador `config`.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                } else {
                    Text("Sem dados ainda.").foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Perfil & objetivos")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
    }

    /// The goal comes from the `config` tab now. It used to read "Recomposição" for
    /// everyone, which stops being true the moment a second person uses the app.
    @ViewBuilder
    private func goalSection(_ r: TodayResponse) -> some View {
        let label = r.basis.goalLabelPt ?? r.caps.goalLabelPt
        Section("Objetivo") {
            HStack(alignment: .firstTextBaseline) {
                Image(systemName: "target").foregroundStyle(Palette.muscle)
                Text(label)
                    .font(.subheadline)
                    .multilineTextAlignment(.leading)
            }
        }
    }

    /// Each number says where it came from. A weight typed into the config tab and
    /// a weight read off a scale are both useful, but they are not the same claim,
    /// and showing them identically would be quietly dishonest.
    @ViewBuilder
    private func bodySection(_ basis: Basis) -> some View {
        Section("Corpo") {
            if basis.weightKg != nil {
                row("Peso", value(basis.weightKg, "kg", decimals: 1),
                    caption: basis.provenance("weight"))
            }
            if basis.leanMassKg != nil {
                row("Massa magra", value(basis.leanMassKg, "kg", decimals: 1),
                    caption: "medido")
            }
            row("Gasto diário (TDEE)", value(basis.tdeeKcal, "kcal", decimals: 0),
                caption: tdeeCaption(basis))
        }
    }

    private func tdeeCaption(_ basis: Basis) -> String {
        switch basis.sources?["tdee"] {
        case "measured": return "média de 14 dias, medida"
        case "declared": return "estimado a partir do que indicaste"
        case "default":  return "valor por defeito — indica os teus dados no config"
        default:         return "média de 14 dias, medida"
        }
    }

    /// What the app is tracking for this person, and what it deliberately is not.
    /// Without this the absence of a whole screen looks like a bug.
    @ViewBuilder
    private func measuringSection(_ caps: Capabilities) -> some View {
        Section("O que a app acompanha") {
            ForEach(caps.domains, id: \.self) { domain in
                Label(Self.domainLabel(domain), systemImage: "checkmark.circle.fill")
                    .foregroundStyle(Palette.goodText)
                    .font(.subheadline)
            }
            ForEach(caps.blindSpots, id: \.self) { domain in
                Label(Self.domainLabel(domain), systemImage: "circle.dashed")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
            }
        }
    }

    private static func domainLabel(_ domain: String) -> String {
        switch domain {
        case "nutrition": return "Alimentação"
        case "sleep":     return "Sono e recuperação"
        case "activity":  return "Atividade e treino"
        case "body":      return "Composição corporal"
        case "digestion": return "Digestão"
        default:          return domain.capitalized
        }
    }

    @ViewBuilder
    private func targetsSection(_ r: TodayResponse) -> some View {
        Section("Objetivos diários") {
            if let cal = r.targets["calories"] {
                row("Calorias",
                    "\(int(cal.floor))–\(int(cal.ceiling)) kcal",
                    caption: "alvo ~\(int(r.basis.calorieTargetKcal)) · défice ~\(int(r.basis.calorieDeficitPct))%")
            }
            if let p = r.targets["protein_g"] {
                row("Proteína", "\(int(p.floor)) g",
                    caption: "\(fmt(r.basis.proteinGPerKg, 1)) g por kg de peso")
            }
            if let f = r.targets["fat_g"] {
                row("Gordura", "≥ \(int(f.floor)) g", caption: "mínimo p/ saúde hormonal")
            }
            if let c = r.targets["carbs_g"] {
                row("Hidratos", "\(int(c.floor))–\(int(c.ceiling)) g", caption: "preenche a energia restante")
            }
            if let fib = r.targets["fiber_g"] {
                row("Fibra", "\(int(fib.floor)) g")
            }
        }
    }

    // MARK: - row helpers

    private func row(_ title: String, _ value: String, caption: String? = nil) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if let caption {
                    Text(caption).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Text(value)
                .font(.body.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }

    private func value(_ v: Double?, _ unit: String, decimals: Int) -> String {
        guard let v else { return "—" }
        return v.formatted(.number.precision(.fractionLength(decimals))) + " " + unit
    }

    private func int(_ v: Double?) -> String {
        guard let v else { return "—" }
        return "\(Int(v.rounded()))"
    }

    private func fmt(_ v: Double?, _ decimals: Int) -> String {
        guard let v else { return "—" }
        return v.formatted(.number.precision(.fractionLength(decimals)))
    }
}
