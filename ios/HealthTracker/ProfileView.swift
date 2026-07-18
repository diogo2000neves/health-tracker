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
                    goalSection(r.basis)
                    bodySection(r.basis)
                    targetsSection(r)
                    Section {
                        Text("Estes objetivos são calculados a partir dos teus próprios dados e atualizam-se sozinhos. Para ajustes finos, edita o separador `targets` na folha de cálculo.")
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

    @ViewBuilder
    private func goalSection(_ basis: Basis) -> some View {
        Section("Objetivo") {
            HStack {
                Label("Recomposição", systemImage: "figure.strengthtraining.traditional")
                    .foregroundStyle(Palette.muscle)
                Spacer()
                Text("perder gordura, manter músculo")
                    .font(.caption).foregroundStyle(.secondary)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    @ViewBuilder
    private func bodySection(_ basis: Basis) -> some View {
        Section("Corpo (medido)") {
            row("Peso", value(basis.weightKg, "kg", decimals: 1))
            row("Massa magra", value(basis.leanMassKg, "kg", decimals: 1))
            row("Gasto diário (TDEE)", value(basis.tdeeKcal, "kcal", decimals: 0),
                caption: "média de 14 dias, medida")
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
