//
//  Coach.swift
//  HealthTracker
//
//  The shapes the coach serves: GET /coach/feed (a stream of cards, each written in
//  the background and valid for a known window), the chat threads hanging off those
//  cards, and the long-term memory.
//
//  Decoded WITHOUT .convertFromSnakeCase (like the rest of the app): explicit
//  CodingKeys keep the mapping honest, and the dictionary-keyed payloads elsewhere
//  would be silently rewritten by the convert strategy.
//
//  EVERY field is decoded defensively. The previous version had one `try decode` on a
//  nested object, and a single report the model phrased differently made the whole
//  response fail to decode — which the UI then rendered as a blank screen. A card
//  that arrives slightly wrong should degrade to a card with less on it, never to an
//  empty tab.
//

import Foundation
import SwiftUI

// MARK: - GET /coach/feed

struct CoachFeed: Decodable {
    /// "ready" once anything has been generated, "empty" before the first run.
    let status: String
    let generatedAt: String?
    let serverTime: String?
    /// The newest card is old enough to be worth refreshing. The app acts on this by
    /// kicking a *background* generation — never by blocking the screen.
    let stale: Bool
    /// A worker is running a generation *right now*, so the app can show real
    /// progress instead of an empty screen.
    let generating: Bool
    /// Generations waiting for the Mac's Sonnet to pick them up. Deliberately
    /// distinct from `generating`: a job can legitimately wait hours for a sleeping
    /// laptop, and showing a progress bar for that would be the old "loading forever"
    /// bug wearing a new hat. This gets a quiet note instead.
    let queued: Int
    let cards: [CoachCard]

    var isEmpty: Bool { cards.isEmpty }

    enum CodingKeys: String, CodingKey {
        case status, stale, generating, queued, cards
        case generatedAt = "generated_at"
        case serverTime = "server_time"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "empty"
        generatedAt = try c.decodeIfPresent(String.self, forKey: .generatedAt)
        serverTime = try c.decodeIfPresent(String.self, forKey: .serverTime)
        stale = try c.decodeIfPresent(Bool.self, forKey: .stale) ?? false
        generating = try c.decodeIfPresent(Bool.self, forKey: .generating) ?? false
        queued = try c.decodeIfPresent(Int.self, forKey: .queued) ?? 0
        cards = try c.decodeIfPresent([CoachCard].self, forKey: .cards) ?? []
    }

    /// An empty feed that is not an error — what the UI shows on a fresh install
    /// while the first generation runs.
    static let placeholder = CoachFeed(status: "empty", cards: [])

    private init(status: String, cards: [CoachCard]) {
        self.status = status
        self.cards = cards
        generatedAt = nil
        serverTime = nil
        stale = true
        generating = false
        queued = 0
    }
}

/// The 202 from `POST /coach/refresh`. `queued == false` means the work couldn't be
/// handed off (the queue is unreachable) — the app stays quiet about it and keeps
/// showing the cards it has.
struct CoachRefreshAck: Decodable {
    let queued: Bool
    let slot: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        queued = try c.decodeIfPresent(Bool.self, forKey: .queued) ?? false
        slot = try c.decodeIfPresent(String.self, forKey: .slot)
    }

    enum CodingKeys: String, CodingKey { case queued, slot }

    #if DEBUG
    static let sample = CoachRefreshAck(queued: true, slot: "adhoc")
    private init(queued: Bool, slot: String?) {
        self.queued = queued
        self.slot = slot
    }
    #endif
}

// MARK: - One card

struct CoachCard: Decodable, Identifiable, Hashable {
    let id: String
    let kindRaw: String
    let slot: String?
    let date: String?
    /// The food group or finding the card is about ("fish_white"), when it has one.
    let topic: String?
    let createdAt: String?
    let priority: Double
    let title: String
    let body: String
    let chips: [CoachChip]
    /// A concrete replacement. The backend only lets one through when the `from` is a
    /// food actually in the log and the `to` was one of the options it offered.
    let swap: CoachSwap?
    /// Only on a `next_meal` card: the ranked plates, delivered WITH the card so
    /// opening the suggestions costs nothing.
    let plates: [Plate]
    let nextSlot: String?
    let threadId: String?

    var kind: CoachCardKind { CoachCardKind(kindRaw) }

    enum CodingKeys: String, CodingKey {
        case id, kind, slot, date, topic, priority, title, body, chips, swap, plates
        case createdAt = "created_at"
        case nextSlot = "next_slot"
        case threadId = "thread_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        kindRaw = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        slot = try c.decodeIfPresent(String.self, forKey: .slot)
        date = try c.decodeIfPresent(String.self, forKey: .date)
        topic = try c.decodeIfPresent(String.self, forKey: .topic)
        createdAt = try c.decodeIfPresent(String.self, forKey: .createdAt)
        priority = try c.decodeIfPresent(Double.self, forKey: .priority) ?? 0
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        body = try c.decodeIfPresent(String.self, forKey: .body) ?? ""
        chips = try c.decodeIfPresent([CoachChip].self, forKey: .chips) ?? []
        swap = try c.decodeIfPresent(CoachSwap.self, forKey: .swap)
        plates = try c.decodeIfPresent([Plate].self, forKey: .plates) ?? []
        nextSlot = try c.decodeIfPresent(String.self, forKey: .nextSlot)
        threadId = try c.decodeIfPresent(String.self, forKey: .threadId)
    }

    /// Nothing to render — dropped rather than shown as an empty card.
    var isBlank: Bool { title.isEmpty && body.isEmpty && plates.isEmpty }
}

/// The card kinds, with their visual language. An unrecognised kind still renders
/// (generically) so a backend that learns a new card type doesn't make the tab go
/// quiet until the app is updated.
enum CoachCardKind: String {
    case dayPlan = "day_plan"
    case checkIn = "check_in"
    case daySummary = "day_summary"
    case weeklyReview = "weekly_review"
    case nextMeal = "next_meal"
    case pattern
    case win
    case unknown

    init(_ raw: String) {
        self = CoachCardKind(rawValue: raw) ?? .unknown
    }

    var label: String {
        switch self {
        case .dayPlan: return "O TEU DIA"
        case .checkIn: return "COMO VAI O DIA"
        case .daySummary: return "DIA FECHADO"
        case .weeklyReview: return "A TUA SEMANA"
        case .nextMeal: return "A PRÓXIMA REFEIÇÃO"
        case .pattern: return "UM PADRÃO"
        case .win: return "A CORRER BEM"
        case .unknown: return "DO COACH"
        }
    }

    var icon: String {
        switch self {
        case .dayPlan: return "sun.horizon.fill"
        case .checkIn: return "clock.badge.checkmark.fill"
        case .daySummary: return "moon.stars.fill"
        case .weeklyReview: return "calendar"
        case .nextMeal: return "fork.knife"
        case .pattern: return "chart.bar.doc.horizontal.fill"
        case .win: return "checkmark.seal.fill"
        case .unknown: return "sparkles"
        }
    }

    var tint: Color {
        switch self {
        case .win: return Palette.goodText
        case .pattern: return Palette.warningText
        case .nextMeal: return Palette.accentText
        case .weeklyReview: return Palette.protein
        default: return Palette.accentText
        }
    }
}

struct CoachChip: Decodable, Identifiable, Hashable {
    let label: String
    let tone: String
    var id: String { label + tone }

    enum CodingKeys: String, CodingKey { case label, tone }

    init(from decoder: Decoder) throws {
        // Tolerates both {"label":…,"tone":…} and a bare string, so a model that
        // simplifies the shape still produces chips instead of none.
        if let single = try? decoder.singleValueContainer().decode(String.self) {
            label = single
            tone = "neutral"
            return
        }
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        tone = try c.decodeIfPresent(String.self, forKey: .tone) ?? "neutral"
    }

    var color: Color {
        switch tone {
        case "good": return Palette.goodText
        case "warn": return Palette.warningText
        case "bad": return Palette.criticalText
        default: return .secondary
        }
    }
}

struct CoachSwap: Decodable, Hashable {
    let from: String
    let to: String
    let why: String
    /// True when the replacement is a food the user hasn't logged — so the UI can say
    /// "novo" out loud instead of pretending it's already a habit.
    let isNew: Bool

    enum CodingKeys: String, CodingKey { case from, to, why, new }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.from = try c.decodeIfPresent(String.self, forKey: .from) ?? ""
        to = try c.decodeIfPresent(String.self, forKey: .to) ?? ""
        why = try c.decodeIfPresent(String.self, forKey: .why) ?? ""
        isNew = try c.decodeIfPresent(Bool.self, forKey: .new) ?? false
    }

    var isUsable: Bool { !from.isEmpty && !to.isEmpty }
}

// MARK: - Plates (carried on a next_meal card)

/// One suggested plate. `rank == 1` is the pick; the others are alternatives.
/// Portions are ranges the backend computed, never the model's guess.
struct Plate: Decodable, Identifiable, Hashable {
    let rank: Int
    let recommended: Bool
    let title: String
    let items: [PlateItem]
    let covers: [Cover]
    let calories: Double?
    let proteinG: Double?
    let why: String?

    var id: Int { rank }

    enum CodingKeys: String, CodingKey {
        case rank, recommended, title, items, covers, calories, why
        case proteinG = "protein_g"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rank = try c.decodeIfPresent(Int.self, forKey: .rank) ?? 1
        recommended = try c.decodeIfPresent(Bool.self, forKey: .recommended) ?? false
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        items = try c.decodeIfPresent([PlateItem].self, forKey: .items) ?? []
        covers = try c.decodeIfPresent([Cover].self, forKey: .covers) ?? []
        calories = try c.decodeIfPresent(Double.self, forKey: .calories)
        proteinG = try c.decodeIfPresent(Double.self, forKey: .proteinG)
        why = try c.decodeIfPresent(String.self, forKey: .why)
    }
}

struct PlateItem: Decodable, Identifiable, Hashable {
    let food: String
    let gramsLow: Int
    let gramsHigh: Int
    let isNew: Bool

    var id: String { food }

    /// "120–150 g" — the range that meaningfully closes the gap.
    var portionText: String {
        gramsLow == gramsHigh ? "\(gramsLow) g" : "\(gramsLow)–\(gramsHigh) g"
    }

    enum CodingKeys: String, CodingKey {
        case food
        case gramsLow = "grams_low"
        case gramsHigh = "grams_high"
        case isNew = "new"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        food = try c.decodeIfPresent(String.self, forKey: .food) ?? ""
        gramsLow = try c.decodeIfPresent(Int.self, forKey: .gramsLow) ?? 0
        gramsHigh = try c.decodeIfPresent(Int.self, forKey: .gramsHigh) ?? gramsLow
        isNew = try c.decodeIfPresent(Bool.self, forKey: .isNew) ?? false
    }
}

/// What a plate fixes — the nutrient it closes, with a short human note.
struct Cover: Decodable, Identifiable, Hashable {
    let key: String?
    let label: String
    let note: String?
    var id: String { (key ?? "") + label }

    enum CodingKeys: String, CodingKey { case key, label, note }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decodeIfPresent(String.self, forKey: .key)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        note = try c.decodeIfPresent(String.self, forKey: .note)
    }
}

// MARK: - Chat

struct CoachThread: Decodable {
    let id: String
    let cardId: String?
    let title: String?
    let turns: [CoachTurn]
    /// True while a question is queued and Sonnet hasn't answered it yet. Derived
    /// server-side from the live job queue, so it can't get stuck on: a job that is
    /// finished, swept or abandoned simply stops being reported.
    let pending: Bool

    enum CodingKeys: String, CodingKey {
        case id, title, turns, pending
        case cardId = "card_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        cardId = try c.decodeIfPresent(String.self, forKey: .cardId)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        turns = try c.decodeIfPresent([CoachTurn].self, forKey: .turns) ?? []
        pending = try c.decodeIfPresent(Bool.self, forKey: .pending) ?? false
    }
}

struct CoachTurn: Decodable, Identifiable, Hashable {
    let role: String
    let text: String
    let at: String?

    /// Turns are appended in pairs and never edited, so position + stamp is a stable
    /// identity without the backend having to mint one.
    var id: String { (at ?? "") + role + text.prefix(24) }
    var isUser: Bool { role == "user" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        role = try c.decodeIfPresent(String.self, forKey: .role) ?? "coach"
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        at = try c.decodeIfPresent(String.self, forKey: .at)
    }

    /// A locally-appended turn, so the user's message appears the instant they send
    /// it rather than after the round trip.
    init(role: String, text: String, at: String? = nil) {
        self.role = role
        self.text = text
        self.at = at
    }

    enum CodingKeys: String, CodingKey { case role, text, at }
}

/// The acknowledgement of a sent question — NOT an answer.
///
/// Chat is queued work: the backend records the question, parks it for Sonnet and
/// returns immediately. The answer arrives in the thread later, which is why this
/// carries the transcript-so-far and a `pending` flag instead of a reply.
struct CoachChatReply: Decodable {
    let threadId: String
    let status: String
    let turns: [CoachTurn]
    let pending: Bool

    /// True when the backend recognised this as a question it already has — a
    /// retried send, a double tap. Nothing was queued twice.
    var wasDuplicate: Bool { status == "already-asked" || status == "already-queued" }

    enum CodingKeys: String, CodingKey {
        case turns, status, pending
        case threadId = "thread_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        threadId = try c.decodeIfPresent(String.self, forKey: .threadId) ?? ""
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "queued"
        turns = try c.decodeIfPresent([CoachTurn].self, forKey: .turns) ?? []
        pending = try c.decodeIfPresent(Bool.self, forKey: .pending) ?? true
    }
}

// MARK: - Memory

struct CoachMemory: Decodable {
    let facts: [CoachFact]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        facts = try c.decodeIfPresent([CoachFact].self, forKey: .facts) ?? []
    }

    enum CodingKeys: String, CodingKey { case facts }

    static let empty = CoachMemory(facts: [])
    private init(facts: [CoachFact]) { self.facts = facts }
}

struct CoachFact: Decodable, Identifiable, Hashable {
    let id: String
    let type: String
    let fact: String
    let source: String?
    let pinned: Bool
    let mentions: Int

    /// pt-PT label for the kind of thing this is, for the memory screen.
    var typeLabel: String {
        switch type {
        case "dislike": return "Não gosta"
        case "preference": return "Preferência"
        case "constraint": return "Restrição"
        case "goal": return "Objetivo"
        case "routine": return "Rotina"
        default: return "Contexto"
        }
    }

    /// Whether the user stated this themselves (as opposed to it being inferred from
    /// a conversation) — worth showing, because it says how much to trust it.
    var isFromUser: Bool { source == "user" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        type = try c.decodeIfPresent(String.self, forKey: .type) ?? "context"
        fact = try c.decodeIfPresent(String.self, forKey: .fact) ?? ""
        source = try c.decodeIfPresent(String.self, forKey: .source)
        pinned = try c.decodeIfPresent(Bool.self, forKey: .pinned) ?? false
        mentions = try c.decodeIfPresent(Int.self, forKey: .mentions) ?? 1
    }

    enum CodingKeys: String, CodingKey {
        case id, type, fact, source, pinned, mentions
    }
}

// MARK: - Dates

/// Parse a backend stamp, with or without a UTC offset.
///
/// The coach's stamps come from a timezone-aware server clock, so they carry an
/// offset ("…T15:30:00+01:00"); the app's older payloads don't. One helper handles
/// both rather than each view guessing.
func coachDate(_ raw: String?) -> Date? {
    guard let raw, !raw.isEmpty else { return nil }
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime]
    if let date = iso.date(from: raw) { return date }

    let naive = DateFormatter()
    naive.calendar = Calendar(identifier: .gregorian)
    naive.locale = Locale(identifier: "en_US_POSIX")
    naive.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    return naive.date(from: String(raw.prefix(19)))
}

/// "há 5 min", "há 2 h", "ontem" — how fresh a card is, in the app's language.
func coachRelativeTime(_ raw: String?) -> String? {
    guard let date = coachDate(raw) else { return nil }
    let seconds = -date.timeIntervalSinceNow
    if seconds < 90 { return "agora" }
    if seconds < 3600 { return "há \(Int(seconds / 60)) min" }
    if seconds < 79_200 { return "há \(Int(seconds / 3600)) h" }
    let days = Int(seconds / 86_400)
    return days <= 1 ? "ontem" : "há \(days) dias"
}

// MARK: - History and reports
//
// The feed is deliberately forgetful; the archive is not. These are the shapes the
// history screen reads — everything the coach has said, noticed and reviewed, so the
// saved data is inspectable by the user and not only by the model.

struct CoachHistory: Decodable {
    let from: String
    let to: String
    let count: Int
    let entries: [CoachHistoryEntry]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        from = try c.decodeIfPresent(String.self, forKey: .from) ?? ""
        to = try c.decodeIfPresent(String.self, forKey: .to) ?? ""
        count = try c.decodeIfPresent(Int.self, forKey: .count) ?? 0
        entries = try c.decodeIfPresent([CoachHistoryEntry].self, forKey: .entries) ?? []
    }

    enum CodingKeys: String, CodingKey { case from, to, count, entries }
}

struct CoachHistoryEntry: Decodable, Identifiable, Hashable {
    let id: String
    let kind: String
    let date: String
    let at: String
    let summary: String
    let body: String
    let importance: Double

    /// pt-PT label for what this entry is.
    var kindLabel: String {
        switch kind {
        case "card": return "Conselho"
        case "chat": return "Conversa"
        case "event": return "Aconteceu"
        case "report": return "Revisão"
        default: return "Registo"
        }
    }

    var icon: String {
        switch kind {
        case "card": return "sparkles"
        case "chat": return "bubble.left.and.text.bubble.right.fill"
        case "event": return "calendar.badge.exclamationmark"
        case "report": return "doc.text.fill"
        default: return "circle"
        }
    }

    var tint: Color {
        switch kind {
        case "event": return Palette.warningText
        case "report": return Palette.protein
        case "chat": return Palette.goodText
        default: return Palette.accentText
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        date = try c.decodeIfPresent(String.self, forKey: .date) ?? ""
        at = try c.decodeIfPresent(String.self, forKey: .at) ?? ""
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        body = try c.decodeIfPresent(String.self, forKey: .body) ?? ""
        importance = try c.decodeIfPresent(Double.self, forKey: .importance) ?? 0
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, date, at, summary, body, importance
    }
}

/// A stored weekly, monthly or yearly review. The card in the feed is the doorway;
/// this is the whole thing.
struct CoachReport: Decodable, Identifiable, Hashable {
    let period: String
    let key: String
    let headline: String
    let summary: String
    let wins: [CoachReportWin]
    let focus: CoachReportFocus?
    let mealReviews: [CoachMealReview]
    let events: [CoachReportEvent]
    let adviceReview: CoachAdviceReview?
    let arc: [CoachArcStep]
    let numbers: [String: String]
    let source: String?

    var id: String { "\(period):\(key)" }

    var periodLabel: String {
        switch period {
        case "weekly": return "Semana de \(shortDate(key))"
        case "monthly": return "Mês de \(key)"
        case "yearly": return key
        default: return key
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        period = try c.decodeIfPresent(String.self, forKey: .period) ?? "weekly"
        key = try c.decodeIfPresent(String.self, forKey: .key) ?? ""
        headline = try c.decodeIfPresent(String.self, forKey: .headline) ?? ""
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        wins = try c.decodeIfPresent([CoachReportWin].self, forKey: .wins) ?? []
        focus = try c.decodeIfPresent(CoachReportFocus.self, forKey: .focus)
        mealReviews = try c.decodeIfPresent([CoachMealReview].self,
                                            forKey: .mealReviews) ?? []
        events = try c.decodeIfPresent([CoachReportEvent].self, forKey: .events) ?? []
        adviceReview = try c.decodeIfPresent(CoachAdviceReview.self,
                                             forKey: .adviceReview)
        arc = try c.decodeIfPresent([CoachArcStep].self, forKey: .arc) ?? []
        numbers = try c.decodeIfPresent([String: String].self, forKey: .numbers) ?? [:]
        source = try c.decodeIfPresent(String.self, forKey: .source)
    }

    enum CodingKeys: String, CodingKey {
        case period, key, headline, summary, wins, focus, events, arc, numbers, source
        case mealReviews = "meal_reviews"
        case adviceReview = "advice_review"
    }
}

struct CoachReportsList: Decodable {
    let period: String
    let reports: [CoachReport]

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        period = try c.decodeIfPresent(String.self, forKey: .period) ?? "weekly"
        reports = try c.decodeIfPresent([CoachReport].self, forKey: .reports) ?? []
    }

    enum CodingKeys: String, CodingKey { case period, reports }
}

struct CoachReportWin: Decodable, Identifiable, Hashable {
    let title: String
    let detail: String
    var id: String { title + detail }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }

    enum CodingKeys: String, CodingKey { case title, detail }
}

struct CoachReportFocus: Decodable, Hashable {
    let label: String
    let why: String
    let how: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        why = try c.decodeIfPresent(String.self, forKey: .why) ?? ""
        how = try c.decodeIfPresent(String.self, forKey: .how) ?? ""
    }

    enum CodingKeys: String, CodingKey { case label, why, how }
}

struct CoachMealReview: Decodable, Identifiable, Hashable {
    let what: String
    let verdict: String
    let why: String
    var id: String { what + verdict }

    var color: Color {
        switch verdict {
        case "good": return Palette.goodText
        case "poor": return Palette.criticalText
        default: return Palette.warningText
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        what = try c.decodeIfPresent(String.self, forKey: .what) ?? ""
        verdict = try c.decodeIfPresent(String.self, forKey: .verdict) ?? "mixed"
        why = try c.decodeIfPresent(String.self, forKey: .why) ?? ""
    }

    enum CodingKeys: String, CodingKey { case what, verdict, why }
}

struct CoachReportEvent: Decodable, Identifiable, Hashable {
    let what: String
    let take: String
    var id: String { what }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        what = try c.decodeIfPresent(String.self, forKey: .what) ?? ""
        take = try c.decodeIfPresent(String.self, forKey: .take) ?? ""
    }

    enum CodingKeys: String, CodingKey { case what, take }
}

struct CoachAdviceReview: Decodable, Hashable {
    let landed: [String]
    let ignored: [String]
    let note: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        landed = try c.decodeIfPresent([String].self, forKey: .landed) ?? []
        ignored = try c.decodeIfPresent([String].self, forKey: .ignored) ?? []
        note = try c.decodeIfPresent(String.self, forKey: .note) ?? ""
    }

    enum CodingKeys: String, CodingKey { case landed, ignored, note }
}

struct CoachArcStep: Decodable, Identifiable, Hashable {
    let when: String
    let what: String
    var id: String { when + what }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        when = try c.decodeIfPresent(String.self, forKey: .when) ?? ""
        what = try c.decodeIfPresent(String.self, forKey: .what) ?? ""
    }

    enum CodingKeys: String, CodingKey { case when, what }
}

/// A short pt-PT date, e.g. "20 jul".
func shortDate(_ iso: String) -> String {
    let parser = DateFormatter()
    parser.calendar = Calendar(identifier: .gregorian)
    parser.locale = Locale(identifier: "en_US_POSIX")
    parser.dateFormat = "yyyy-MM-dd"
    guard let date = parser.date(from: iso) else { return iso }
    let out = DateFormatter()
    out.locale = Locale(identifier: "pt_PT")
    out.dateFormat = "d MMM"
    return out.string(from: date)
}
