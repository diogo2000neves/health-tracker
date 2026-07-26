//
//  CoachStore.swift
//  HealthTracker
//
//  The Coach tab's state. Everything here exists because of how the previous version
//  behaved, so the rules are worth stating plainly:
//
//  1. **Render from disk first, always.** The feed is seeded from the last cached
//     response in `init`, so the first frame after launch already has content. The tab
//     can be blank only on a genuinely fresh install — and even then it shows a
//     placeholder with progress, never an empty screen.
//  2. **Never block on the network, never block on a model.** `GET /coach/feed` is a
//     pure storage read on the server (~100 ms). Generation happens in the background
//     via `POST /coach/refresh`, which returns 202 immediately.
//  3. **A failure never destroys good data.** A failed refresh leaves the cached feed
//     on screen and surfaces an error only when there is nothing to show. The old
//     store assigned the response unconditionally, so a "skipped" or empty reply
//     wiped out a perfectly good one.
//  4. **If the user is waiting, show real progress.** While the server reports
//     `generating`, the store polls and advances a determinate progress bar. The old
//     version showed a bare spinner that could spin forever with nothing behind it.
//  5. **Load at most once per interval.** The old code loaded on app launch AND on
//     tab appearance AND on every foreground, then triggered generation from inside
//     the load — three overlapping 45-second Gemini calls for one tap.
//

import Foundation
import SwiftUI

@MainActor
@Observable
final class CoachStore {
    /// Seeded from disk at init, so the very first frame already has cards.
    var feed: CoachFeed?
    /// Only set when there is nothing at all to show — a refresh that fails behind
    /// existing cards is invisible by design.
    var errorMessage: String?
    /// True only while there is nothing cached yet (fresh install).
    var isLoading = false
    /// A reload running behind cards that are already on screen.
    var isRefreshing = false
    /// The server is generating. Drives the progress bar.
    var isGenerating = false
    /// 0…1 while generating, so the user sees movement rather than a mystery spinner.
    var progress: Double = 0
    /// Card ids the user has already seen, so genuinely new cards can be marked.
    private(set) var seenCardIDs: Set<String> = []

    private var lastLoadedAt: Date?
    private var pollTask: Task<Void, Never>?
    private var generationStartedAt: Date?

    /// A reload sooner than this after the last one is skipped, unless forced (pull to
    /// refresh always forces). Foregrounding the app three times in a minute should
    /// not cost three round trips.
    private let minReloadInterval: TimeInterval = 45

    /// How long a generation usually takes end to end. Only used to pace the progress
    /// bar; the real completion signal is the server saying it finished.
    private let expectedGenerationSeconds: Double = 25

    /// Poll pacing and ceiling while a generation is in flight.
    private let pollInterval: Duration = .seconds(3)
    private let pollCeiling: TimeInterval = 120

    private let seenKey = "coach.seenCardIDs"

    init() {
        feed = APIClient.shared.cachedCoachFeed()
        seenCardIDs = Set(UserDefaults.standard.stringArray(forKey: seenKey) ?? [])
    }

    var cards: [CoachCard] {
        (feed?.cards ?? []).filter { !$0.isBlank }
    }

    /// Nothing to show and nothing cached — the only state that gets a placeholder.
    var isEmpty: Bool { cards.isEmpty }

    func isNew(_ card: CoachCard) -> Bool { !seenCardIDs.contains(card.id) }

    /// How many cards the user hasn't looked at yet — the tab badge, and the reason to
    /// open the app tomorrow.
    var unseenCount: Int { cards.filter { isNew($0) }.count }

    func markSeen(_ cards: [CoachCard]) {
        let ids = cards.map(\.id)
        guard !ids.allSatisfy({ seenCardIDs.contains($0) }) else { return }
        seenCardIDs.formUnion(ids)
        // Bounded: ids are dated, so old ones can never come back and be "new" again.
        UserDefaults.standard.set(Array(seenCardIDs.suffix(400)), forKey: seenKey)
    }

    // MARK: - Loading

    /// Read the feed. Cheap, safe to call on appearance and on foreground.
    ///
    /// `force` bypasses the interval guard (pull to refresh) and asks the server for a
    /// regeneration even if the feed isn't stale yet.
    func load(force: Bool = false) async {
        if !force, let last = lastLoadedAt,
           Date().timeIntervalSince(last) < minReloadInterval, feed != nil {
            return
        }

        let had = !(feed?.cards.isEmpty ?? true)
        if had { isRefreshing = true } else { isLoading = true }
        defer { isLoading = false; isRefreshing = false }

        do {
            let fresh = try await APIClient.shared.coachFeed()
            apply(fresh)
            lastLoadedAt = Date()
            errorMessage = nil

            // Ask for new cards when the feed has gone stale, or when the user pulled
            // to refresh. Fire and forget: the work happens on the server and the
            // poll below is what notices it landing.
            if fresh.stale || force {
                await requestRefresh(reason: force ? "manual" : "stale")
            } else if fresh.generating {
                startPolling()
            }
        } catch {
            // A background refresh failing just leaves the last-known-good cards on
            // screen; only an empty tab is allowed to show an error.
            if !had { errorMessage = error.localizedDescription }
        }
    }

    /// Called when a meal is logged elsewhere in the app: the day changed, so the
    /// time-sensitive cards are now describing a day that no longer exists.
    func mealWasLogged() {
        Task { await requestRefresh(reason: "meal_logged") }
    }

    /// Ask the server to regenerate, and start watching for the result.
    private func requestRefresh(reason: String) async {
        guard !isGenerating else { return }
        do {
            _ = try await APIClient.shared.coachRefresh(reason: reason)
            beginGenerating()
        } catch {
            // The queue is unreachable. There is nothing for the user to do about it
            // and the cached cards are still on screen, so this stays quiet.
        }
    }

    private func apply(_ fresh: CoachFeed) {
        // Only replace what's on screen with something that actually has cards. An
        // empty response behind a populated feed means "generation hasn't landed
        // yet", not "you have nothing".
        if fresh.cards.isEmpty, !(feed?.cards.isEmpty ?? true) {
            if !fresh.generating { endGenerating() }
            return
        }
        feed = fresh
        if !fresh.generating { endGenerating() }
    }

    // MARK: - Generation progress

    private func beginGenerating() {
        isGenerating = true
        generationStartedAt = Date()
        progress = 0.05
        startPolling()
    }

    private func endGenerating() {
        pollTask?.cancel()
        pollTask = nil
        guard isGenerating else { return }
        isGenerating = false
        generationStartedAt = nil
        withAnimation(.easeOut(duration: 0.25)) { progress = 1 }
    }

    /// Poll the feed while the server works, advancing the progress bar.
    ///
    /// The bar is paced against a typical generation and capped short of full, because
    /// a bar that sits at 100% while nothing happens is worse than one that is honestly
    /// still moving. It only completes when the server says it did.
    private func startPolling() {
        pollTask?.cancel()
        isGenerating = true
        if generationStartedAt == nil { generationStartedAt = Date() }

        pollTask = Task { [weak self] in
            let started = Date()
            while !Task.isCancelled {
                try? await Task.sleep(for: self?.pollInterval ?? .seconds(3))
                guard let self, !Task.isCancelled else { return }

                let elapsed = Date().timeIntervalSince(started)
                withAnimation(.linear(duration: 0.3)) {
                    self.progress = min(0.92, 0.05
                                        + elapsed / self.expectedGenerationSeconds * 0.87)
                }

                if let fresh = try? await APIClient.shared.coachFeed() {
                    let landed = fresh.cards.map(\.id) != (self.feed?.cards.map(\.id) ?? [])
                    self.apply(fresh)
                    if !fresh.generating || landed {
                        self.endGenerating()
                        self.lastLoadedAt = Date()
                        return
                    }
                }

                if elapsed > self.pollCeiling {
                    // Give up watching, keep whatever is on screen. The generation may
                    // still land; the next open will pick it up.
                    self.endGenerating()
                    return
                }
            }
        }
    }

    // No `deinit` cancel: the task captures `self` weakly and returns as soon as it
    // wakes to find the store gone, so it cannot outlive it by more than one poll
    // interval — and a main-actor `deinit` isn't allowed to touch isolated state
    // anyway.
}

// MARK: - Chat

/// One conversation with the coach, anchored to a card.
///
/// Kept separate from `CoachStore` so a chat that is slow or fails can never affect
/// the feed, and so the sheet holds its own lifecycle.
@MainActor
@Observable
final class CoachChatStore {
    let card: CoachCard
    var turns: [CoachTurn] = []
    var isSending = false
    var isLoadingHistory = false
    var errorMessage: String?
    /// Set briefly when the coach learned something durable about the user, so the UI
    /// can say so — memory that changes silently is memory the user can't correct.
    var learnedSomething = false

    private var threadID: String { card.threadId ?? "" }

    init(card: CoachCard) {
        self.card = card
        turns = APIClient.shared.cachedCoachThread(id: card.threadId ?? "")?.turns ?? []
    }

    var canChat: Bool { !threadID.isEmpty }

    func loadHistory() async {
        guard canChat, turns.isEmpty else { return }
        isLoadingHistory = true
        defer { isLoadingHistory = false }
        if let thread = try? await APIClient.shared.coachThread(id: threadID) {
            turns = thread.turns
        }
    }

    func send(_ text: String) async {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canChat, !message.isEmpty, !isSending else { return }

        // Optimistic: the user's own words appear immediately. If the send fails the
        // turn is rolled back, so the transcript never lies about what was sent.
        turns.append(CoachTurn(role: "user", text: message))
        isSending = true
        errorMessage = nil
        defer { isSending = false }

        do {
            let reply = try await APIClient.shared.coachChat(
                threadID: threadID, cardID: card.id, message: message)
            turns = reply.turns.isEmpty
                ? turns + [CoachTurn(role: "coach", text: reply.reply)]
                : reply.turns
            if reply.memoryLearned > 0 { learnedSomething = true }
        } catch {
            turns.removeLast()
            errorMessage = error.localizedDescription
        }
    }
}

// MARK: - Memory

@MainActor
@Observable
final class CoachMemoryStore {
    var facts: [CoachFact] = []
    var isLoading = false
    var errorMessage: String?

    init() {
        facts = APIClient.shared.cachedCoachMemory()?.facts ?? []
    }

    func load() async {
        let had = !facts.isEmpty
        if !had { isLoading = true }
        defer { isLoading = false }
        do {
            facts = try await APIClient.shared.coachMemory().facts
            errorMessage = nil
        } catch {
            if !had { errorMessage = error.localizedDescription }
        }
    }

    func add(_ fact: String, type: String) async {
        guard !fact.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        if let updated = try? await APIClient.shared.addCoachMemory(fact: fact,
                                                                   type: type) {
            facts = updated.facts
        }
    }

    func forget(_ fact: CoachFact) async {
        // Optimistic removal: the user asked for this to be gone, so it goes now.
        let previous = facts
        facts.removeAll { $0.id == fact.id }
        do {
            facts = try await APIClient.shared.deleteCoachMemory(id: fact.id).facts
        } catch {
            facts = previous
            errorMessage = error.localizedDescription
        }
    }
}
