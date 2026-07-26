//
//  CoachChatView.swift
//  HealthTracker
//
//  A conversation about one card. The card that started it stays pinned at the top, so
//  the thread never drifts from the thing it is about, and the coach's side of the
//  conversation is answered with the same facts that produced the card.
//
//  Every turn is stored server-side under the card's thread id, which is derived from
//  the card itself — so reopening the same card continues one conversation instead of
//  forking a new one.
//

import SwiftUI

struct CoachChatView: View {
    @State var store: CoachChatStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 14) {
                            AnchorCard(card: store.card)

                            if store.turns.isEmpty && !store.isLoadingHistory {
                                Suggestions { question in
                                    Task { await store.send(question) }
                                }
                            }

                            ForEach(store.turns) { turn in
                                TurnBubble(turn: turn)
                                    .id(turn.id)
                            }

                            if store.isSending {
                                TypingBubble()
                                    .id("typing")
                            }

                            if let error = store.errorMessage {
                                Text(error)
                                    .font(.footnote)
                                    .foregroundStyle(Palette.criticalText)
                                    .frame(maxWidth: .infinity, alignment: .center)
                            }
                        }
                        .padding(16)
                    }
                    .onChange(of: store.turns.count) { _, _ in
                        scrollToEnd(proxy)
                    }
                    .onChange(of: store.isSending) { _, sending in
                        if sending { withAnimation { proxy.scrollTo("typing") } }
                    }
                }

                if store.learnedSomething {
                    LearnedBanner()
                }

                composer
            }
            .background(Palette.screen)
            .navigationTitle("Coach")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Fechar") { dismiss() }
                }
            }
        }
        .presentationDetents([.large])
        .task { await store.loadHistory() }
    }

    private var composer: some View {
        HStack(spacing: 10) {
            TextField("Pergunta o que quiseres…", text: $draft, axis: .vertical)
                .lineLimit(1...4)
                .textFieldStyle(.plain)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Palette.card, in: Capsule())
                .focused($inputFocused)
                .submitLabel(.send)
                .onSubmit(send)

            Button(action: send) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
                    .foregroundStyle(canSend ? Palette.accent : Color.secondary)
            }
            .disabled(!canSend)
            .accessibilityLabel("Enviar")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !store.isSending && store.canChat
    }

    private func send() {
        guard canSend else { return }
        let message = draft
        draft = ""
        Task { await store.send(message) }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        guard let last = store.turns.last else { return }
        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
    }
}

// MARK: - Pieces

/// The card the conversation is about, quoted at the top so the thread can't drift.
struct AnchorCard: View {
    let card: CoachCard

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: card.kind.icon)
                    .font(.caption2)
                    .foregroundStyle(card.kind.tint)
                Text(card.kind.label)
                    .font(.caption2.weight(.semibold))
                    .tracking(0.5)
                    .foregroundStyle(.secondary)
            }
            if !card.title.isEmpty {
                Text(card.title)
                    .font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !card.body.isEmpty {
                Text(card.body)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .card(padding: 14)
    }
}

/// Openers, so the first message doesn't have to be invented from scratch.
struct Suggestions: View {
    let onPick: (String) -> Void

    private let questions = [
        "Porque é que isto importa?",
        "Dá-me uma alternativa prática",
        "Não gosto disso — o que mais posso fazer?",
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Podes perguntar")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.tertiary)
            ForEach(questions, id: \.self) { question in
                Button {
                    onPick(question)
                } label: {
                    HStack {
                        Text(question)
                            .font(.subheadline)
                            .multilineTextAlignment(.leading)
                        Spacer(minLength: 0)
                        Image(systemName: "arrow.up.right")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 9)
                    .background(Palette.card, in: RoundedRectangle(cornerRadius: 12,
                                                                   style: .continuous))
                }
                .buttonStyle(.plain)
                .foregroundStyle(Palette.accentText)
            }
        }
    }
}

struct TurnBubble: View {
    let turn: CoachTurn

    var body: some View {
        HStack {
            if turn.isUser { Spacer(minLength: 40) }
            Text(turn.text)
                .font(.subheadline)
                .foregroundStyle(turn.isUser ? Color.white : Color.primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(turn.isUser ? Palette.accent : Palette.card,
                            in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                .fixedSize(horizontal: false, vertical: true)
            if !turn.isUser { Spacer(minLength: 40) }
        }
    }
}

struct TypingBubble: View {
    @State private var phase = 0.0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(Color.secondary)
                    .frame(width: 6, height: 6)
                    .opacity(phase == Double(index) ? 1 : 0.35)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Palette.card, in: RoundedRectangle(cornerRadius: 18,
                                                       style: .continuous))
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear {
            withAnimation(.easeInOut(duration: 0.5).repeatForever()) { phase = 2 }
        }
        .accessibilityLabel("A escrever")
    }
}

/// Says out loud when the coach has remembered something. Memory that changes silently
/// is memory the user can't correct.
struct LearnedBanner: View {
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "brain")
                .font(.caption)
                .foregroundStyle(Palette.accentText)
            Text("Guardei isso sobre ti — podes ver e apagar no ícone do cérebro.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Palette.accent.opacity(0.10))
    }
}

#if DEBUG
#Preview("Chat") {
    CoachChatView(store: CoachChatStore(card: SampleData.coachFeed.cards[1]))
}
#endif
