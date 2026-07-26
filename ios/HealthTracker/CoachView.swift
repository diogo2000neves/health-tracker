//
//  CoachView.swift
//  HealthTracker
//
//  The Coach tab: a feed of cards the server wrote in the background, newest and most
//  urgent first. Each card is one idea about the FOOD the user actually logged — a
//  routine worth noticing, a plate for the next meal, the week in review — and each
//  one can be talked to.
//
//  The three UI rules that come straight from what was wrong before:
//    * There is always something on screen. Cached cards render on the first frame;
//      an empty feed shows a placeholder that explains itself, never a blank page.
//    * Work in progress is always visible, with a determinate bar. A spinner that can
//      spin forever is worse than no spinner.
//    * A card is never a dead end: the swap, the plates and the chat entry are all
//      right there on it.
//

import SwiftUI
import UIKit

struct CoachView: View {
    let store: CoachStore
    @State private var chatCard: CoachCard?
    @State private var showMemory = false

    var body: some View {
        NavigationStack {
            ZStack(alignment: .top) {
                content
                // The progress bar lives above the content rather than replacing it,
                // so a refresh never takes the cards away from the user.
                if store.isGenerating {
                    GenerationBar(progress: store.progress)
                        .transition(.move(edge: .top).combined(with: .opacity))
                } else if store.isQueued {
                    QueuedNote()
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .background(Palette.screen)
            .navigationTitle("Coach")
            .toolbar {
                if store.isRefreshing && !store.isGenerating {
                    ToolbarItem(placement: .topBarTrailing) { SyncIndicator() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showMemory = true
                    } label: {
                        Image(systemName: "brain")
                    }
                    .accessibilityLabel("O que o coach sabe de ti")
                }
            }
            .refreshable { await store.load(force: true) }
            // `sheet(item:)` builds the sheet FROM the tapped card. The old next-meal
            // sheet took a snapshot of the store at presentation time instead, so a
            // suggestion that arrived a moment later never appeared and the sheet sat
            // on "preparing…" forever.
            .sheet(item: $chatCard) { card in
                CoachChatView(store: CoachChatStore(card: card))
            }
            .sheet(isPresented: $showMemory) {
                CoachMemoryView(store: CoachMemoryStore())
            }
        }
        .task { await store.load() }
        .onDisappear { store.markSeen(store.cards) }
    }

    @ViewBuilder
    private var content: some View {
        if !store.cards.isEmpty {
            feed
        } else if store.isLoading || store.isGenerating || store.isQueued {
            firstRun
        } else if let error = store.errorMessage {
            LoadingOrError(isLoading: false, error: error) {
                Task { await store.load(force: true) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            waiting
        }
    }

    private var feed: some View {
        ScrollView {
            VStack(spacing: 14) {
                if store.isGenerating || store.isQueued {
                    // Reserve the banner's height so the first card doesn't jump.
                    Spacer().frame(height: 26)
                }
                ForEach(store.cards) { card in
                    CoachCardView(card: card, isNew: store.isNew(card)) {
                        chatCard = card
                    }
                }
                FeedFooter(generatedAt: store.feed?.generatedAt)
            }
            .padding(16)
        }
    }

    /// Nothing cached yet, with work under way. The one moment a placeholder is
    /// honest — and it says which of the two waits this is.
    private var firstRun: some View {
        ContentUnavailableView {
            Label("A preparar o teu coach", systemImage: "sparkles")
        } description: {
            Text(store.isGenerating
                 ? "Estou a ler as tuas últimas semanas de refeições para te dizer "
                   + "algo que valha a pena."
                 : "Pedi uma análise ao modelo grande. Aparece aqui assim que estiver "
                   + "pronta — podes fechar a app à vontade.")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    /// No cards and nothing running: usually a brand-new log with too little in it.
    private var waiting: some View {
        ContentUnavailableView {
            Label("Ainda sem novidades", systemImage: "sparkles")
        } description: {
            Text("Preciso de alguns dias de refeições registadas para ver padrões que "
                 + "valham a pena contar. Continua a registar — apareço aqui sozinho.")
        } actions: {
            Button {
                Task { await store.load(force: true) }
            } label: {
                Label("Procurar novidades", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Generation progress

/// A thin determinate bar pinned under the navigation bar. Determinate on purpose:
/// the user asked to know that something is actually being fetched.
struct GenerationBar: View {
    let progress: Double

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles")
                    .font(.caption2)
                    .foregroundStyle(Palette.accentText)
                Text("A preparar novidades…")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 0)
            }
            ProgressView(value: min(max(progress, 0), 1))
                .progressViewStyle(.linear)
                .tint(Palette.accent)
        }
        .padding(.horizontal, 16)
        .padding(.top, 6)
        .padding(.bottom, 8)
        .background(.bar)
    }
}

/// Work is queued for the Mac's Sonnet, which may be asleep. No bar and no
/// percentage: this can take hours, and the honest thing is to say so and get out of
/// the way rather than animate a promise the app can't keep.
struct QueuedNote: View {
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "hourglass")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text("Análise pedida ao modelo grande — chega quando estiver pronta")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

// MARK: - A card

struct CoachCardView: View {
    let card: CoachCard
    let isNew: Bool
    let onChat: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header

            if !card.title.isEmpty {
                Text(card.title)
                    .font(.system(.title3, design: .rounded).weight(.semibold))
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !card.body.isEmpty {
                Text(card.body)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !card.chips.isEmpty {
                FlowLayout(spacing: 8) {
                    ForEach(card.chips) { chip in
                        FactChip(chip: chip)
                    }
                }
            }

            if let swap = card.swap, swap.isUsable {
                SwapRow(swap: swap)
            }

            if !card.plates.isEmpty {
                PlatesStrip(plates: card.plates)
            }

            if card.threadId != nil {
                Divider()
                Button(action: onChat) {
                    HStack(spacing: 6) {
                        Image(systemName: "bubble.left.and.text.bubble.right.fill")
                            .font(.footnote)
                        Text("Falar sobre isto")
                            .font(.subheadline.weight(.semibold))
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.tertiary)
                    }
                    .foregroundStyle(Palette.accentText)
                }
                .buttonStyle(.plain)
            }
        }
        .card(padding: 18)
        .overlay(alignment: .topTrailing) {
            if isNew {
                Circle()
                    .fill(Palette.accent)
                    .frame(width: 8, height: 8)
                    .padding(12)
                    .accessibilityLabel("Novo")
            }
        }
    }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: card.kind.icon)
                .font(.caption)
                .foregroundStyle(card.kind.tint)
            Text(card.kind.label)
                .font(.caption.weight(.semibold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
            if let when = coachRelativeTime(card.createdAt) {
                Text(when)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
    }
}

struct FactChip: View {
    let chip: CoachChip

    var body: some View {
        Text(chip.label)
            .font(.caption.weight(.medium))
            .foregroundStyle(chip.color)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(chip.color.opacity(0.12), in: Capsule())
    }
}

/// The swap, compact enough to live inside a card. The backend guarantees the `from`
/// is a food the user has actually logged, so this can be stated plainly.
struct SwapRow: View {
    let swap: CoachSwap

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            side(caption: "Em vez de", food: swap.from, color: .primary,
                 strikethrough: true, isNew: false)
            Image(systemName: "arrow.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
            side(caption: "Experimenta", food: swap.to, color: Palette.goodText,
                 strikethrough: false, isNew: swap.isNew)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.track, in: RoundedRectangle(cornerRadius: 14,
                                                        style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private func side(caption: String, food: String, color: Color,
                      strikethrough: Bool, isNew: Bool) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(caption.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
            HStack(spacing: 5) {
                Text(food)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(color)
                    .strikethrough(strikethrough, color: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if isNew {
                    Text("novo")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(Palette.accentText)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Palette.accent.opacity(0.15), in: Capsule())
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Plates on a next-meal card

/// The plates ride along on the card, so there is nothing to fetch when the user taps
/// — the exact opposite of the old sheet, which opened empty and waited on a model.
struct PlatesStrip: View {
    let plates: [Plate]
    @State private var expanded: Int?

    var body: some View {
        VStack(spacing: 10) {
            ForEach(plates.sorted { $0.rank < $1.rank }) { plate in
                PlateRow(plate: plate, isExpanded: expanded == plate.rank) {
                    withAnimation(.snappy(duration: 0.22)) {
                        expanded = expanded == plate.rank ? nil : plate.rank
                    }
                }
            }
        }
    }
}

struct PlateRow: View {
    let plate: Plate
    let isExpanded: Bool
    let onTap: () -> Void
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button(action: onTap) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(plate.title)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.primary)
                            .fixedSize(horizontal: false, vertical: true)
                        macros
                    }
                    Spacer(minLength: 0)
                    if plate.recommended || plate.rank == 1 {
                        Text("RECOMENDADO")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(Palette.accentText)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(Palette.accent.opacity(0.15), in: Capsule())
                    }
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                }
            }
            .buttonStyle(.plain)

            if isExpanded {
                if !plate.items.isEmpty {
                    FlowLayout(spacing: 8) {
                        ForEach(plate.items) { item in
                            IngredientChip(item: item)
                        }
                    }
                }
                if !plate.covers.isEmpty {
                    FlowLayout(spacing: 8) {
                        ForEach(plate.covers) { cover in
                            CoverPill(cover: cover)
                        }
                    }
                }
                if let why = plate.why, !why.isEmpty {
                    Text(why)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    UIPasteboard.general.string = plateAsText
                    withAnimation { copied = true }
                } label: {
                    Label(copied ? "Copiado" : "Copiar para registar",
                          systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.footnote.weight(.semibold))
                }
                .buttonStyle(.bordered)
                .tint(copied ? Palette.good : Palette.accent)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.track.opacity(0.6),
                    in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }

    private var macros: some View {
        HStack(spacing: 10) {
            if let cal = plate.calories {
                Label("\(Int(cal)) kcal", systemImage: "flame.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let protein = plate.proteinG {
                Label("\(Int(protein)) g proteína", systemImage: "bolt.fill")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The plate as plain text, for pasting into a meal note when logging it.
    private var plateAsText: String {
        let items = plate.items.map { "\($0.food) \($0.portionText)" }
            .joined(separator: ", ")
        return items.isEmpty ? plate.title : "\(plate.title): \(items)"
    }
}

/// One ingredient with its portion range; a new-to-you food carries a small badge.
struct IngredientChip: View {
    let item: PlateItem

    var body: some View {
        HStack(spacing: 6) {
            Text(item.food).font(.footnote.weight(.medium))
            Text(item.portionText)
                .font(.footnote)
                .foregroundStyle(.secondary)
            if item.isNew {
                Text("novo")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(Palette.accentText)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Palette.accent.opacity(0.15), in: Capsule())
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Palette.card, in: Capsule())
    }
}

struct CoverPill: View {
    let cover: Cover

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: "checkmark.circle.fill")
                .font(.caption2)
                .foregroundStyle(Palette.good)
            Text(coverText)
                .font(.caption.weight(.medium))
                .foregroundStyle(Palette.goodText)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(Palette.good.opacity(0.12), in: Capsule())
    }

    private var coverText: String {
        if let note = cover.note, !note.isEmpty { return "\(cover.label) · \(note)" }
        return cover.label
    }
}

// MARK: - Footer

struct FeedFooter: View {
    let generatedAt: String?

    var body: some View {
        VStack(spacing: 2) {
            if let when = coachRelativeTime(generatedAt) {
                Text("Atualizado \(when)")
            }
            Text("Novidades de manhã, à tarde e à noite")
        }
        .font(.caption2)
        .foregroundStyle(.tertiary)
        .frame(maxWidth: .infinity)
        .padding(.top, 4)
    }
}

#if DEBUG
private func sampleCoachStore() -> CoachStore {
    let store = CoachStore()
    store.feed = SampleData.coachFeed
    return store
}

#Preview("Feed") {
    CoachView(store: sampleCoachStore())
}

#Preview("Card — pattern") {
    ScrollView {
        VStack(spacing: 14) {
            ForEach(SampleData.coachFeed.cards) { card in
                CoachCardView(card: card, isNew: card.kind == .pattern) {}
            }
        }
        .padding(16)
    }
    .background(Palette.screen)
}
#endif
