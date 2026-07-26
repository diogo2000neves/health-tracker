//
//  CoachMemoryView.swift
//  HealthTracker
//
//  What the coach remembers about you, in plain sight and editable.
//
//  This screen is not a nicety. Memory is what makes the daily cards get more personal
//  instead of just more frequent, and it is assembled partly from what the model
//  *inferred* during conversations. An inference that is wrong would otherwise shape
//  every future suggestion invisibly and forever — so every fact is listed, labelled by
//  where it came from, and deletable in one swipe.
//

import SwiftUI

struct CoachMemoryView: View {
    @State var store: CoachMemoryStore
    @Environment(\.dismiss) private var dismiss
    @State private var draft = ""
    @State private var draftType = "preference"

    private let types: [(String, String)] = [
        ("preference", "Preferência"),
        ("dislike", "Não gosto"),
        ("constraint", "Restrição"),
        ("goal", "Objetivo"),
        ("routine", "Rotina"),
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(store.facts) { fact in
                        FactRow(fact: fact)
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) {
                                    Task { await store.forget(fact) }
                                } label: {
                                    Label("Esquecer", systemImage: "trash")
                                }
                            }
                    }
                } header: {
                    Text("O que sei de ti")
                } footer: {
                    if store.facts.isEmpty {
                        Text("Ainda não sei nada sobre ti. À medida que falares comigo "
                             + "nos cartões, vou guardando o que for útil — e podes "
                             + "sempre apagar o que não fizer sentido.")
                    } else {
                        Text("Uso isto em tudo o que te escrevo. Desliza para a "
                             + "esquerda para esquecer algo.")
                    }
                }

                Section("Dizer-me algo") {
                    Picker("Tipo", selection: $draftType) {
                        ForEach(types, id: \.0) { value, label in
                            Text(label).tag(value)
                        }
                    }
                    HStack {
                        TextField("ex.: não como lactose à noite", text: $draft)
                            .submitLabel(.done)
                            .onSubmit(add)
                        Button("Guardar", action: add)
                            .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
            }
            .navigationTitle("Memória do coach")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Fechar") { dismiss() }
                }
            }
            .overlay {
                if store.isLoading && store.facts.isEmpty {
                    ProgressView()
                }
            }
        }
        .task { await store.load() }
    }

    private func add() {
        let fact = draft
        draft = ""
        Task { await store.add(fact, type: draftType) }
    }
}

struct FactRow: View {
    let fact: CoachFact

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(fact.fact)
                .font(.subheadline)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                Text(fact.typeLabel)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Palette.accentText)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Palette.accent.opacity(0.14), in: Capsule())
                // Where a fact came from is worth showing: "disseste-me" is stronger
                // evidence than something inferred from a conversation.
                Text(fact.isFromUser ? "disseste-me" : "notei numa conversa")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                if fact.mentions > 1 {
                    Text("· \(fact.mentions)×")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

#if DEBUG
#Preview("Memory") {
    CoachMemoryView(store: CoachMemoryStore())
}
#endif
