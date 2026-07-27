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
//  3. **A failure never destroys good data.** A failed refresh throws and leaves the
//     cached feed on screen, surfacing an error only when there is nothing to show.
//     A *successful* empty answer is different and is taken at face value: the server
//     filters cards by what the current part of the day is about, so "nothing" in the
//     evening genuinely means this morning's check-in no longer applies.
//  4. **Only claim progress that exists.** A determinate bar runs while a worker is
//     actually generating. Work merely queued for the Mac's Sonnet gets a quiet note
//     instead — it can honestly wait hours for a sleeping laptop, and a bar promising
//     imminent results would be the old forever-spinner in a nicer costume.
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
    /// A worker is actively generating. Drives the progress bar.
    var isGenerating = false
    /// Work is queued for the Mac's Sonnet, which may be asleep. Deliberately NOT a
    /// progress bar: this can honestly take hours, and a bar that promises imminent
    /// results is the old "loading forever" lie in a nicer costume.
    var isQueued = false
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

            // Ask for new cards when the feed has gone stale — by age or because the
            // day has moved into a part these cards have nothing to say about — or
            // when the user pulled to refresh. Fire and forget: the work happens on
            // the server, and the poll below is what notices it landing.
            if fresh.stale || force {
                await requestRefresh(reason: force ? "manual" : "stale")
            } else if fresh.generating || fresh.queued > 0 {
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
            // Queued, not generating: the request goes to the Mac first, and it may
            // be asleep. Poll for a while in case it answers quickly.
            isQueued = true
            startPolling()
        } catch {
            // The queue is unreachable. There is nothing for the user to do about it
            // and the cached cards are still on screen, so this stays quiet.
        }
    }

    private func apply(_ fresh: CoachFeed) {
        // The server's list is authoritative, including when it is empty.
        //
        // It filters cards by what the current part of the day is actually about, so
        // an empty answer in the evening means "this morning's check-in is no longer
        // the truth", not "the response was lost". Keeping the old cards on screen
        // here would resurrect exactly the stale-context problem the filter exists to
        // fix. A genuinely failed refresh never reaches this method — it throws, and
        // `load` leaves the cached feed alone.
        feed = fresh
        isQueued = fresh.queued > 0
        if fresh.generating {
            if !isGenerating { beginGenerating() }
        } else {
            endGenerating()
        }
    }

    // MARK: - Generation progress

    private func beginGenerating() {
        isGenerating = true
        generationStartedAt = Date()
        progress = 0.05
        startPolling()
    }

    private func endGenerating() {
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
        if generationStartedAt == nil { generationStartedAt = Date() }

        pollTask = Task { [weak self] in
            let started = Date()
            while !Task.isCancelled {
                try? await Task.sleep(for: self?.pollInterval ?? .seconds(3))
                guard let self, !Task.isCancelled else { return }

                if self.isGenerating {
                    let elapsed = Date().timeIntervalSince(started)
                    withAnimation(.linear(duration: 0.3)) {
                        self.progress = min(0.92, 0.05
                                            + elapsed / self.expectedGenerationSeconds * 0.87)
                    }
                }

                if let fresh = try? await APIClient.shared.coachFeed() {
                    let landed = fresh.cards.map(\.id) != (self.feed?.cards.map(\.id) ?? [])
                    self.apply(fresh)
                    if landed || (!fresh.generating && fresh.queued == 0) {
                        self.endGenerating()
                        self.lastLoadedAt = Date()
                        return
                    }
                }

                // Stop watching after a couple of minutes. Work queued for a sleeping
                // Mac can take hours, and that is fine — it lands in the feed whenever
                // it lands, and the next time the app opens it is simply there.
                if Date().timeIntervalSince(started) > self.pollCeiling {
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

    /// True while a question of ours is queued and unanswered. Drives the "a
    /// pensar…" bubble, and survives leaving the screen: it is re-derived from the
    /// backend on every load, not remembered locally.
    var isAwaitingReply = false

    private var threadID: String { card.threadId ?? "" }
    private var pollTask: Task<Void, Never>?

    init(card: CoachCard) {
        self.card = card
        turns = APIClient.shared.cachedCoachThread(id: card.threadId ?? "")?.turns ?? []
    }

    var canChat: Bool { !threadID.isEmpty }

    /// Stop watching for an answer — the screen is gone. The question is not
    /// cancelled by this: it stays queued, gets answered, and is waiting in the
    /// thread next time the chat is opened.
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func loadHistory() async {
        guard canChat else { return }
        // Always refresh, even with turns already on screen: an answer may have
        // landed while the app was closed, which is the normal case now that Sonnet
        // answers in its own time.
        isLoadingHistory = turns.isEmpty
        defer { isLoadingHistory = false }
        if let thread = try? await APIClient.shared.coachThread(id: threadID) {
            turns = thread.turns
            isAwaitingReply = thread.pending
            if thread.pending { startPolling() }
        }
    }

    func send(_ text: String) async {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canChat, !message.isEmpty, !isSending else { return }

        // One id per message the user composes — NOT per HTTP attempt. The retry
        // inside APIClient reuses it, so a lost response can never become a second
        // question. This is the whole fix for the duplicate-message bug.
        let turnID = UUID().uuidString.replacingOccurrences(of: "-", with: "")

        // Optimistic: the user's own words appear immediately. If the send fails the
        // turn is rolled back, so the transcript never lies about what was sent.
        turns.append(CoachTurn(role: "user", text: message))
        isSending = true
        errorMessage = nil
        defer { isSending = false }

        do {
            let ack = try await APIClient.shared.coachChat(
                threadID: threadID, cardID: card.id, message: message,
                turnID: turnID)
            // The backend's transcript is authoritative — it has already de-duped.
            if !ack.turns.isEmpty { turns = ack.turns }
            isAwaitingReply = ack.pending
            if ack.pending { startPolling() }
        } catch {
            turns.removeLast()
            errorMessage = error.localizedDescription
        }
    }

    /// Watch for the answer while the screen is open.
    ///
    /// Sonnet answers when the Mac is awake and inside its usage window, so this can
    /// be seconds or much longer. Polling backs off and then gives up rather than
    /// running forever — the answer is safe in the thread either way, and reopening
    /// the chat picks it up. Nothing here can duplicate anything: it only reads.
    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            // ~2 minutes of watching, easing off as hope fades.
            let delays: [UInt64] = [3, 3, 5, 5, 8, 8, 12, 15, 20, 30]
            for delay in delays {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                if Task.isCancelled { return }
                guard let self else { return }
                guard let thread = try? await APIClient.shared.coachThread(
                    id: self.threadID) else { continue }
                if Task.isCancelled { return }
                self.turns = thread.turns
                self.isAwaitingReply = thread.pending
                if !thread.pending { return }
            }
            // Still nothing: stop the spinner rather than implying it is imminent.
            self?.isAwaitingReply = false
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
