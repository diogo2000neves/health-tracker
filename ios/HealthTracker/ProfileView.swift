//
//  ProfileView.swift
//  HealthTracker
//
//  Perfil & objetivos — the latest measured biometrics and the fixed daily plan,
//  shown read-only. The targets are constants in the backend (see
//  backend/ingest/main.py's _fixed_targets), not derived from anything here.
//

import SwiftUI

struct ProfileView: View {
    let store: TodayStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                if let r = store.response {
                    bodySection(r.basis)
                    targetsSection(r)
                    measuringSection(r.caps)
                    Section {
                        Text("Estes objetivos são fixos e não mudam de dia para dia. Para os alterar, edita as constantes no backend; para mudar o que a app acompanha, o separador `config` na folha de cálculo.")
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

    /// The latest measured biometrics — real per-day readings, unrelated to the
    /// fixed targets below.
    @ViewBuilder
    private func bodySection(_ basis: Basis) -> some View {
        Section("Corpo") {
            if basis.weightKg != nil {
                row("Peso", value(basis.weightKg, "kg", decimals: 1), caption: "medido")
            }
            if basis.leanMassKg != nil {
                row("Massa magra", value(basis.leanMassKg, "kg", decimals: 1),
                    caption: "medido")
            }
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
                row("Calorias", "\(int(cal.floor))–\(int(cal.ceiling)) kcal",
                    caption: "meta fixa ~\(int(r.basis.calorieTargetKcal)) kcal")
            }
            if let p = r.targets["protein_g"] {
                row("Proteína", "\(int(p.floor)) g", caption: "preserva massa muscular")
            }
            if let f = r.targets["fat_g"] {
                row("Gordura", "\(int(f.floor)) g", caption: "mínimo p/ saúde hormonal")
            }
            if let c = r.targets["carbs_g"] {
                row("Hidratos", "\(int(c.floor)) g", caption: "energia para os treinos")
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
}
