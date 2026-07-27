//
//  CoachHistoryView.swift
//  HealthTracker
//
//  Everything the coach has kept: the reviews it has written, and the running record
//  of what it advised, what it noticed and what you talked about.
//
//  The feed forgets by design — a stale "what to eat next" is worse than none — and
//  that used to mean the advice was gone the moment it expired. It isn't any more.
//  This screen exists so the archive is inspectable by the person it is about, rather
//  than only by the model reading it.
//

import SwiftUI

@MainActor
@Observable
final class CoachHistoryStore {
    var entries: [CoachHistoryEntry] = []
    var reports: [CoachReport] = []
    var isLoading = false
    var errorMessage: String?
    var period = "weekly" {
        didSet { Task { await loadReports() } }
    }

    init() {
        entries = APIClient.shared.cachedCoachHistory()?.entries ?? []
        reports = APIClient.shared.cachedCoachReports(period: "weekly")?.reports ?? []
    }

    func load() async {
        let had = !entries.isEmpty || !reports.isEmpty
        if !had { isLoading = true }
        defer { isLoading = false }
        do {
            async let history = APIClient.shared.coachHistory()
            async let list = APIClient.shared.coachReports(period: period)
            entries = try await history.entries
            reports = try await list.reports
            errorMessage = nil
        } catch {
            if !had { errorMessage = error.localizedDescription }
        }
    }

    private func loadReports() async {
        if let list = try? await APIClient.shared.coachReports(period: period) {
            reports = list.reports
        }
    }

    /// History grouped by day, newest first — the shape a person reads it in.
    var byDay: [(day: String, entries: [CoachHistoryEntry])] {
        let grouped = Dictionary(grouping: entries) { $0.date }
        return grouped.keys.sorted(by: >).map { ($0, grouped[$0] ?? []) }
    }
}

struct CoachHistoryView: View {
    @State var store: CoachHistoryStore
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    @State private var openReport: CoachReport?

    var body: some View {
        NavigationStack {
            // A VStack, not a Group: modifiers on a Group are applied to each child
            // in turn, which gave the screen two "Fechar" buttons — one from the
            // picker, one from the list.
            VStack(spacing: 0) {
                Picker("", selection: $tab) {
                    Text("Revisões").tag(0)
                    Text("Tudo").tag(1)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.bottom, 4)

                if tab == 0 { reportsList } else { timeline }
            }
            .background(Palette.screen)
            .navigationTitle("Histórico")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Fechar") { dismiss() }
                }
            }
            .overlay {
                if store.isLoading && store.entries.isEmpty && store.reports.isEmpty {
                    ProgressView()
                }
            }
            .sheet(item: $openReport) { report in
                CoachReportView(report: report)
            }
        }
        .task { await store.load() }
    }

    private var reportsList: some View {
        ScrollView {
            VStack(spacing: 12) {
                Picker("Período", selection: Binding(
                    get: { store.period }, set: { store.period = $0 })) {
                    Text("Semanas").tag("weekly")
                    Text("Meses").tag("monthly")
                    Text("Anos").tag("yearly")
                }
                .pickerStyle(.segmented)

                if store.reports.isEmpty {
                    ContentUnavailableView {
                        Label("Ainda sem revisões", systemImage: "doc.text")
                    } description: {
                        Text("A primeira revisão semanal é escrita ao domingo, "
                             + "com a semana toda à frente: as refeições, os "
                             + "conselhos que te dei e as conversas que tivemos.")
                    }
                    .padding(.top, 40)
                } else {
                    ForEach(store.reports) { report in
                        Button { openReport = report } label: {
                            ReportRow(report: report)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(16)
        }
    }

    private var timeline: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 18) {
                if store.entries.isEmpty {
                    ContentUnavailableView("Ainda sem histórico", systemImage: "clock",
                                           description: Text("Tudo o que o coach "
                                                             + "disser fica guardado aqui."))
                        .padding(.top, 40)
                }
                ForEach(store.byDay, id: \.day) { day, entries in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(prettyDate(day))
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(.secondary)
                        ForEach(entries) { entry in
                            HistoryRow(entry: entry)
                        }
                    }
                }
            }
            .padding(16)
        }
    }
}

struct ReportRow: View {
    let report: CoachReport

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "doc.text.fill")
                    .font(.caption)
                    .foregroundStyle(Palette.protein)
                Text(report.periodLabel.uppercased())
                    .font(.caption.weight(.semibold))
                    .tracking(0.5)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            if !report.headline.isEmpty {
                Text(report.headline)
                    .font(.headline)
                    .foregroundStyle(.primary)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let focus = report.focus, !focus.label.isEmpty {
                Label(focus.label, systemImage: "target")
                    .font(.caption)
                    .foregroundStyle(Palette.accentText)
            }
        }
        .card()
    }
}

struct HistoryRow: View {
    let entry: CoachHistoryEntry

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: entry.icon)
                .font(.caption)
                .foregroundStyle(entry.tint)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(entry.kindLabel)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(entry.tint)
                    if !entry.at.isEmpty {
                        Text(entry.at)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                Text(entry.summary)
                    .font(.subheadline)
                    .fixedSize(horizontal: false, vertical: true)
                if !entry.body.isEmpty {
                    Text(entry.body)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .lineLimit(4)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .card(padding: 12)
    }
}

/// One review in full. The card in the feed is the doorway; this is the whole thing —
/// including the part the feed has no room for, which is whether the advice landed.
struct CoachReportView: View {
    let report: CoachReport
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if !report.headline.isEmpty {
                        Text(report.headline)
                            .font(.system(.title2, design: .rounded).weight(.semibold))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !report.summary.isEmpty {
                        Text(report.summary)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if let focus = report.focus, !focus.label.isEmpty {
                        section("O foco", icon: "target") {
                            Text(focus.label).font(.headline)
                            if !focus.why.isEmpty {
                                Text(focus.why).font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                            if !focus.how.isEmpty {
                                Label(focus.how, systemImage: "arrow.right")
                                    .font(.footnote)
                                    .foregroundStyle(Palette.accentText)
                            }
                        }
                    }

                    if !report.mealReviews.isEmpty {
                        section("As refeições", icon: "fork.knife") {
                            ForEach(report.mealReviews) { review in
                                HStack(alignment: .top, spacing: 8) {
                                    Circle().fill(review.color)
                                        .frame(width: 7, height: 7).padding(.top, 6)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(review.what)
                                            .font(.subheadline.weight(.medium))
                                        Text(review.why).font(.footnote)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }

                    if let advice = report.adviceReview,
                       !(advice.landed.isEmpty && advice.ignored.isEmpty) {
                        section("O que pegou", icon: "checkmark.circle") {
                            ForEach(advice.landed, id: \.self) { item in
                                Label(item, systemImage: "checkmark")
                                    .font(.footnote)
                                    .foregroundStyle(Palette.goodText)
                            }
                            ForEach(advice.ignored, id: \.self) { item in
                                Label(item, systemImage: "arrow.uturn.left")
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                            if !advice.note.isEmpty {
                                Text(advice.note).font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }

                    if !report.events.isEmpty {
                        section("Os acontecimentos", icon: "calendar.badge.exclamationmark") {
                            ForEach(report.events) { event in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(event.what)
                                        .font(.subheadline.weight(.medium))
                                    Text(event.take).font(.footnote)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }

                    if !report.arc.isEmpty {
                        section("A trajetória", icon: "chart.line.uptrend.xyaxis") {
                            ForEach(report.arc) { step in
                                HStack(alignment: .top, spacing: 8) {
                                    Text(step.when)
                                        .font(.caption.weight(.semibold))
                                        .foregroundStyle(.tertiary)
                                        .frame(width: 74, alignment: .leading)
                                    Text(step.what).font(.footnote)
                                }
                            }
                        }
                    }

                    if !report.wins.isEmpty {
                        section("O que correu bem", icon: "checkmark.seal.fill") {
                            ForEach(report.wins) { win in
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(win.title).font(.subheadline.weight(.medium))
                                    Text(win.detail).font(.footnote)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }

                    if !report.numbers.isEmpty {
                        FlowLayout(spacing: 8) {
                            ForEach(report.numbers.sorted(by: { $0.key < $1.key }),
                                    id: \.key) { key, value in
                                Text("\(key): \(value)")
                                    .font(.caption)
                                    .padding(.horizontal, 9).padding(.vertical, 5)
                                    .background(Palette.track, in: Capsule())
                            }
                        }
                    }

                    if let source = report.source {
                        Text("Escrito por \(source)")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding(16)
            }
            .background(Palette.screen)
            .navigationTitle(report.periodLabel)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
    }

    @ViewBuilder
    private func section<Content: View>(_ title: String, icon: String,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: title, systemImage: icon, accent: Palette.accentText)
            VStack(alignment: .leading, spacing: 10) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .card()
        }
    }
}
