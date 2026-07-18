//
//  TrendsView.swift
//  HealthTracker
//
//  «Tendências» — "am I improving over weeks?". Led by the recomposition
//  north-star (muscle holding/up while fat and weight come down), then the
//  energy→weight loop, adherence (streaks + a heatmap, on INPUTS only), and finally
//  body outcomes (sleep, recovery) kept deliberately apart — they react to previous
//  days, so they are charted, never streaked or attributed to today.
//

import SwiftUI
import Charts

struct TrendsView: View {
    let store: TrendsStore
    let today: TodayStore

    var body: some View {
        NavigationStack {
            ZStack {
                Palette.screen.ignoresSafeArea()
                if let response = store.response, !response.days.isEmpty {
                    content(response.days)
                } else if store.isLoading || store.response == nil {
                    LoadingOrError(isLoading: store.isLoading,
                                   error: store.errorMessage) {
                        Task { await store.load() }
                    }
                } else {
                    ContentUnavailableView("Sem histórico ainda",
                                           systemImage: "chart.xyaxis.line",
                                           description: Text("Aparece aqui à medida que os dias se acumulam."))
                }
            }
            .navigationTitle("Tendências")
        }
        .task { await store.load() }
    }

    @ViewBuilder
    private func content(_ days: [HealthDay]) -> some View {
        ScrollView {
            VStack(spacing: 16) {
                RecompCard(days: days)
                BodyChartsCard(days: days)
                EnergyBalanceTrendCard(days: days)
                AdherenceCard(days: days, today: today)
                OutcomesCard(days: days)
            }
            .padding(16)
        }
        .refreshable { await store.load() }
    }
}

// MARK: - shared helpers

struct DatedValue: Identifiable {
    let id = UUID()
    let date: Date
    let value: Double
}

struct SeriesPoint: Identifiable {
    let id = UUID()
    let date: Date
    let value: Double
    let series: String
}

private func parseDay(_ iso: String) -> Date? {
    let f = DateFormatter()
    f.calendar = Calendar(identifier: .gregorian)
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd"
    return f.date(from: iso)
}

private func series(_ days: [HealthDay], _ pick: (HealthDay) -> Double?) -> [DatedValue] {
    days.compactMap { day in
        guard let value = pick(day), let date = parseDay(day.date) else { return nil }
        return DatedValue(date: date, value: value)
    }
}

// MARK: - Recomposition north-star

private struct RecompCard: View {
    let days: [HealthDay]

    var body: some View {
        let muscle = series(days) { $0.body?.muscleMassKg }
        let fat = series(days) { $0.body?.bodyFatPct }
        let weight = series(days) { $0.body?.weightKg }

        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Recomposição", systemImage: "figure.strengthtraining.traditional",
                          accent: Palette.muscle)

            HStack(spacing: 10) {
                StatTile(title: "Músculo", latest: muscle.last?.value, delta: delta(muscle),
                         unit: "kg", decimals: 1, upIsGood: true, color: Palette.muscle)
                StatTile(title: "Gordura", latest: fat.last?.value, delta: delta(fat),
                         unit: "%", decimals: 1, upIsGood: false, color: Palette.bodyFat)
                StatTile(title: "Peso", latest: weight.last?.value, delta: delta(weight),
                         unit: "kg", decimals: 1, upIsGood: false, color: Palette.weight,
                         neutral: true)
            }

            Text("Sucesso = músculo a manter-se ou a subir enquanto a gordura desce. O peso desce devagar; lê a tendência, não o valor de um dia.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .card()
    }

    private func delta(_ points: [DatedValue]) -> Double? {
        guard let first = points.first?.value, let last = points.last?.value else { return nil }
        return last - first
    }
}

private struct StatTile: View {
    let title: String
    let latest: Double?
    let delta: Double?
    let unit: String
    var decimals: Int = 1
    let upIsGood: Bool
    let color: Color
    var neutral: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 2) {
                Text(latest.map { $0.formatted(.number.precision(.fractionLength(decimals))) } ?? "—")
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(color)
                Text(unit).font(.caption2).foregroundStyle(.secondary)
            }
            if let delta, !neutral {
                DeltaBadge(delta: delta, unit: "", upIsGood: upIsGood, decimals: decimals)
            } else if let delta {
                Text((delta >= 0 ? "+" : "") + delta.formatted(.number.precision(.fractionLength(decimals))))
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text(" ").font(.caption)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Palette.screen, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

// MARK: - Body composition charts

private struct BodyChartsCard: View {
    let days: [HealthDay]

    var body: some View {
        let weight = series(days) { $0.body?.weightKg }.map { SeriesPoint(date: $0.date, value: $0.value, series: "Peso") }
        let muscle = series(days) { $0.body?.muscleMassKg }.map { SeriesPoint(date: $0.date, value: $0.value, series: "Músculo") }
        let fat = series(days) { $0.body?.bodyFatPct }

        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Peso & massa muscular", systemImage: "scalemass")
            Chart(weight + muscle) { point in
                LineMark(x: .value("Data", point.date),
                         y: .value("kg", point.value))
                    .foregroundStyle(by: .value("Série", point.series))
                    .interpolationMethod(.catmullRom)
                    .lineStyle(StrokeStyle(lineWidth: 2))
            }
            .chartForegroundStyleScale(["Peso": Palette.weight, "Músculo": Palette.muscle])
            .chartYScale(domain: .automatic(includesZero: false))
            .chartXAxis { AxisMarks(values: .stride(by: .month)) {
                AxisGridLine(); AxisValueLabel(format: .dateTime.month(.abbreviated))
            } }
            .chartLegend(position: .top, alignment: .leading)
            .frame(height: 180)

            Divider()

            SectionHeader(title: "Gordura corporal", systemImage: "drop.halffull")
            Chart(fat) { point in
                AreaMark(x: .value("Data", point.date), y: .value("%", point.value))
                    .foregroundStyle(Palette.bodyFat.opacity(0.12))
                    .interpolationMethod(.catmullRom)
                LineMark(x: .value("Data", point.date), y: .value("%", point.value))
                    .foregroundStyle(Palette.bodyFat)
                    .interpolationMethod(.catmullRom)
                    .lineStyle(StrokeStyle(lineWidth: 2))
            }
            .chartYScale(domain: .automatic(includesZero: false))
            .chartXAxis { AxisMarks(values: .stride(by: .month)) {
                AxisGridLine(); AxisValueLabel(format: .dateTime.month(.abbreviated))
            } }
            .frame(height: 150)
        }
        .card()
    }
}

// MARK: - Energy balance → weight loop

private struct EnergyBalanceTrendCard: View {
    let days: [HealthDay]

    var body: some View {
        let points = Array(series(days) { $0.nutrition?.energyBalanceKcal.map(Double.init) }.suffix(30))

        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Balanço energético", systemImage: "bolt.horizontal")
            if points.isEmpty {
                Text("Ainda sem dias completos de energia.")
                    .font(.subheadline).foregroundStyle(.secondary)
            } else {
                let maxVal = points.map { abs($0.value) }.max() ?? 1
                let padded = (maxVal * 1.15).rounded(.up)
                Chart(points) { point in
                    BarMark(x: .value("Data", point.date),
                            y: .value("kcal", point.value))
                        .foregroundStyle(point.value >= 0 ? Palette.critical : Palette.accent)
                        .cornerRadius(2)
                }
                .chartYScale(domain: -padded...padded)
                .chartXAxis { AxisMarks(values: .stride(by: .weekOfYear)) {
                    AxisGridLine(); AxisValueLabel(format: .dateTime.day().month(.abbreviated))
                } }
                .frame(height: 150)
                Text("Défice (azul) abaixo de zero, excedente (vermelho) acima. A composição corporal segue esta linha ao longo de semanas — o efeito aparece nos dias seguintes, não no próprio dia.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .card()
    }
}

// MARK: - Adherence (inputs only)

private struct AdherenceCard: View {
    let days: [HealthDay]
    let today: TodayStore

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Adesão", systemImage: "flame")

            HStack(spacing: 10) {
                StreakTile(title: "Proteína", days: proteinStreak, color: Palette.protein)
                StreakTile(title: "Calorias", days: calorieStreak, color: Palette.accent)
                StreakTile(title: "Fibra", days: fiberStreak, color: Palette.carbs)
            }

            Heatmap(days: Array(days.suffix(28)), rows: heatmapRows)
        }
        .card()
    }

    // thresholds from the current targets (fall back to sensible defaults)
    private var proteinGoal: Double { today.response?.targets["protein_g"]?.goal ?? 140 }
    private var fiberGoal: Double { today.response?.targets["fiber_g"]?.goal ?? 29 }
    private var calFloor: Double { today.response?.targets["calories"]?.floor ?? 1900 }
    private var calCeiling: Double { today.response?.targets["calories"]?.ceiling ?? 2300 }

    private var proteinStreak: Int {
        streak(series(days) { $0.nutrition?.totalProteinG }) { $0 >= proteinGoal }
    }
    private var fiberStreak: Int {
        streak(series(days) { $0.nutrition?.totalFiberG }) { $0 >= fiberGoal }
    }
    private var calorieStreak: Int {
        streak(series(days) { $0.nutrition?.totalCalsIn }) { $0 >= calFloor && $0 <= calCeiling }
    }

    /// Consecutive most-recent days (from the last one backwards) that pass `met`.
    private func streak(_ points: [DatedValue], met: (Double) -> Bool) -> Int {
        var count = 0
        for point in points.reversed() {
            if met(point.value) { count += 1 } else { break }
        }
        return count
    }

    private var heatmapRows: [HeatmapRow] {
        [
            HeatmapRow(label: "Prot.", pick: { $0.nutrition?.totalProteinG },
                       state: { proteinState($0) }),
            HeatmapRow(label: "Cal.", pick: { $0.nutrition?.totalCalsIn },
                       state: { calorieState($0) }),
            HeatmapRow(label: "Fibra", pick: { $0.nutrition?.totalFiberG },
                       state: { fiberState($0) }),
        ]
    }

    private func proteinState(_ v: Double) -> AdherenceState {
        v >= proteinGoal ? .met : (v >= 0.8 * proteinGoal ? .close : .miss)
    }
    private func fiberState(_ v: Double) -> AdherenceState {
        v >= fiberGoal ? .met : (v >= 0.7 * fiberGoal ? .close : .miss)
    }
    private func calorieState(_ v: Double) -> AdherenceState {
        if v >= calFloor && v <= calCeiling { return .met }
        if v >= 0.9 * calFloor && v <= 1.1 * calCeiling { return .close }
        return .miss
    }
}

private struct StreakTile: View {
    let title: String
    let days: Int
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                if days > 0 {
                    Image(systemName: "flame.fill").font(.caption).foregroundStyle(color)
                }
                Text("\(days)")
                    .font(.system(size: 22, weight: .bold, design: .rounded))
                    .monospacedDigit()
                Text(days == 1 ? "dia" : "dias").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Palette.screen, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

enum AdherenceState { case met, close, miss, noData
    var color: Color {
        switch self {
        case .met: return Palette.good
        case .close: return Palette.warning
        case .miss: return Palette.critical.opacity(0.65)
        case .noData: return Color.primary.opacity(0.08)
        }
    }
}

struct HeatmapRow: Identifiable {
    let id = UUID()
    let label: String
    let pick: (HealthDay) -> Double?
    let state: (Double) -> AdherenceState
}

private struct Heatmap: View {
    let days: [HealthDay]
    let rows: [HeatmapRow]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(rows) { row in
                HStack(spacing: 3) {
                    Text(row.label)
                        .font(.caption2).foregroundStyle(.secondary)
                        .frame(width: 40, alignment: .leading)
                    ForEach(days) { day in
                        let state: AdherenceState = row.pick(day).map { row.state($0) } ?? .noData
                        RoundedRectangle(cornerRadius: 2, style: .continuous)
                            .fill(state.color)
                            .frame(height: 14)
                            .frame(maxWidth: .infinity)
                    }
                }
            }
            HStack(spacing: 10) {
                legend(.met, "no alvo")
                legend(.close, "perto")
                legend(.miss, "falhou")
                legend(.noData, "sem dados")
            }
            .padding(.top, 2)
        }
    }

    private func legend(_ state: AdherenceState, _ label: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 2).fill(state.color).frame(width: 10, height: 10)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
    }
}

// MARK: - Outcomes (observed, kept causally separate)

private struct OutcomesCard: View {
    let days: [HealthDay]

    var body: some View {
        let sleep = series(days) { $0.sleep?.sleepMins.map(Double.init) }
        let restingHr = series(days) { $0.recovery?.restingHrBpm.map(Double.init) }
        let hrv = series(days) { $0.recovery?.hrvMs }

        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Resultados do corpo", systemImage: "bed.double")
            HStack(spacing: 10) {
                OutcomeTile(title: "Sono", value: sleep.last.map { sleepText($0.value) } ?? "—",
                            spark: sleep, color: Palette.accent)
                OutcomeTile(title: "FC repouso", value: restingHr.last.map { "\(Int($0.value))" } ?? "—",
                            spark: restingHr, color: Palette.bodyFat)
                OutcomeTile(title: "HRV", value: hrv.last.map { "\(Int($0.value))" } ?? "—",
                            spark: hrv, color: Palette.muscle)
            }
            Text("Reagem aos dias anteriores — por isso são observados, não pontuados nem atribuídos ao próprio dia.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .card()
    }

    private func sleepText(_ mins: Double) -> String {
        let h = Int(mins) / 60, m = Int(mins) % 60
        return "\(h)h\(String(format: "%02d", m))"
    }
}

private struct OutcomeTile: View {
    let title: String
    let value: String
    let spark: [DatedValue]
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 20, weight: .bold, design: .rounded))
                .monospacedDigit()
            Chart(spark.suffix(21)) { point in
                LineMark(x: .value("d", point.date), y: .value("v", point.value))
                    .foregroundStyle(color)
                    .interpolationMethod(.catmullRom)
                    .lineStyle(StrokeStyle(lineWidth: 1.5))
            }
            .chartXAxis(.hidden)
            .chartYAxis(.hidden)
            .chartYScale(domain: .automatic(includesZero: false))
            .frame(height: 28)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Palette.screen, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
