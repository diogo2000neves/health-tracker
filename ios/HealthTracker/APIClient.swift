//
//  APIClient.swift
//  HealthTracker
//
//  One tiny async client over the ingest service. Attaches the shared-secret header
//  and decodes /today and /daily.
//
//  Decoding deliberately uses a PLAIN JSONDecoder (no .convertFromSnakeCase): the
//  responses carry dictionaries keyed by the backend's snake_case metric names
//  (consumed, targets, per-item nutrients), and the convert strategy would rewrite
//  those dictionary keys too, silently breaking every lookup. Structs map their
//  fields with explicit CodingKeys instead.
//

import Foundation

enum APIError: LocalizedError {
    case badStatus(Int)
    case notHTTP

    var errorDescription: String? {
        switch self {
        case .badStatus(401):
            return "Não autorizado — verifica o token em Config.swift."
        case .badStatus(404):
            return "Endpoint não encontrado — o backend pode não estar atualizado."
        case .badStatus(let code):
            return "O servidor respondeu \(code)."
        case .notHTTP:
            return "Resposta inesperada do servidor."
        }
    }
}

struct APIClient {
    static let shared = APIClient()

    /// The live daily payload: consumed macros+micros, per-metric targets, and the
    /// day's meals with per-ingredient nutrients. Defaults to today (server tz).
    func today(date: String? = nil) async throws -> TodayResponse {
        if useSampleData { return SampleData.today }
        return try await get("today", query: date.map { [URLQueryItem(name: "date", value: $0)] } ?? [])
    }

    /// Days from daily_summary for the trends screen. Defaults to the last 30 days;
    /// pass a wider `from` for longer history.
    func daily(from: String? = nil, to: String? = nil) async throws -> DailyResponse {
        if useSampleData { return SampleData.daily }
        var query: [URLQueryItem] = []
        if let from { query.append(URLQueryItem(name: "from", value: from)) }
        if let to { query.append(URLQueryItem(name: "to", value: to)) }
        return try await get("daily", query: query)
    }

    /// The per-nutrient reference knowledge base (static; fetched once and cached).
    func nutrients() async throws -> NutrientInfoResponse {
        if useSampleData { return SampleData.nutrients }
        return try await get("nutrients", query: [])
    }

    // MARK: - Coach
    //
    // The read is a plain GET of already-generated cards — the server does no model
    // work on this path, so it answers in ~100 ms and the app never waits on Gemini
    // to draw a screen. Generation is asked for separately and acknowledged with a
    // 202; the app watches for the result by re-reading the feed.

    /// The current card feed. Always answers, generated or not.
    func coachFeed() async throws -> CoachFeed {
        if useSampleData { return SampleData.coachFeed }
        return try await get("coach/feed", query: [], cacheAs: "coach_feed")
    }

    /// Ask for fresh cards. Returns as soon as the work is queued (202) — never waits
    /// for the generation itself.
    @discardableResult
    func coachRefresh(reason: String) async throws -> CoachRefreshAck {
        if useSampleData { return CoachRefreshAck.sample }
        return try await post("coach/refresh", body: ["reason": reason],
                              timeout: 20)
    }

    /// One conversation's history.
    func coachThread(id: String) async throws -> CoachThread {
        if useSampleData { return SampleData.coachThread }
        return try await get("coach/thread/\(id)", query: [],
                            cacheAs: "coach_thread_\(id)")
    }

    /// Send a message about a card.
    ///
    /// Returns as soon as the question is RECORDED, not answered — no model runs in
    /// this request, so it is fast and there is nothing for a timeout to interrupt.
    /// The answer appears in the thread later, once Sonnet has written it.
    ///
    /// `turnID` is what makes the send safe to retry: one id per message the user
    /// composes, so a lost response, a flaky connection or a double tap all land on
    /// the same id and the backend recognises them as the same question. The previous
    /// version had neither the id nor the async shape, and a single tap that outran
    /// the 60 s timeout became three questions with three different answers.
    func coachChat(threadID: String, cardID: String, message: String,
                   turnID: String) async throws -> CoachChatReply {
        if useSampleData { return SampleData.coachChatReply(message: message) }
        return try await post("coach/chat",
                              body: ["thread_id": threadID, "card_id": cardID,
                                     "message": message,
                                     "client_turn_id": turnID],
                              timeout: 30)
    }

    /// Everything the coach has said, noticed and reviewed. Keeping all of it is the
    /// point of the archive; the history screen is how a human checks that it is
    /// really being kept.
    func coachHistory(from: String? = nil, to: String? = nil,
                      kinds: String? = nil) async throws -> CoachHistory {
        if useSampleData { return SampleData.coachHistory }
        var query: [URLQueryItem] = []
        if let from { query.append(URLQueryItem(name: "from", value: from)) }
        if let to { query.append(URLQueryItem(name: "to", value: to)) }
        if let kinds { query.append(URLQueryItem(name: "kinds", value: kinds)) }
        return try await get("coach/history", query: query, cacheAs: "coach_history")
    }

    /// Past weekly / monthly / yearly reviews, newest first.
    func coachReports(period: String) async throws -> CoachReportsList {
        if useSampleData { return SampleData.coachReports }
        return try await get("coach/reports",
                             query: [URLQueryItem(name: "period", value: period)],
                             cacheAs: "coach_reports_\(period)")
    }

    /// What the coach remembers about the user.
    func coachMemory() async throws -> CoachMemory {
        if useSampleData { return SampleData.coachMemory }
        return try await get("coach/memory", query: [], cacheAs: "coach_memory")
    }

    func addCoachMemory(fact: String, type: String) async throws -> CoachMemory {
        if useSampleData { return SampleData.coachMemory }
        return try await post("coach/memory", body: ["fact": fact, "type": type])
    }

    func deleteCoachMemory(id: String) async throws -> CoachMemory {
        if useSampleData { return SampleData.coachMemory }
        var request = URLRequest(url: Config.baseURL.appending(path: "coach/memory/\(id)"))
        request.httpMethod = "DELETE"
        request.setValue(Config.authToken, forHTTPHeaderField: "X-Auth-Token")
        return try await send(request, cacheAs: nil)
    }

    /// Hand-correct one ingredient's numbers on an already-logged meal (e.g. the AI
    /// overestimated a food's protein). Returns the updated meal; the caller still
    /// refetches `today()` afterward (see TodayStore.load) to keep the day's totals
    /// and rings in sync rather than hand-mutating the immutable response structs.
    func editMealItem(datetime: String, itemIndex: Int, calories: Double? = nil,
                      protein: Double? = nil, carbs: Double? = nil, fat: Double? = nil,
                      portionG: Double? = nil) async throws -> TodayMeal {
        var body: [String: Any] = ["datetime": datetime, "item_index": itemIndex]
        if let calories { body["calories"] = calories }
        if let protein { body["protein_g"] = protein }
        if let carbs { body["carbs_g"] = carbs }
        if let fat { body["fat_g"] = fat }
        if let portionG { body["portion_g"] = portionG }
        return try await post("meals/edit", body: body)
    }

    // MARK: - Disk-cached last-known-good (read synchronously at store init, so the
    // UI has something to show before the first network round trip even starts).

    func cachedToday() -> TodayResponse? {
        useSampleData ? nil : DiskCache.load(TodayResponse.self, as: "today")
    }

    func cachedDaily() -> DailyResponse? {
        useSampleData ? nil : DiskCache.load(DailyResponse.self, as: "daily")
    }

    func cachedNutrients() -> NutrientInfoResponse? {
        useSampleData ? nil : DiskCache.load(NutrientInfoResponse.self, as: "nutrients")
    }

    /// The last feed the app saw. Read synchronously in `CoachStore.init`, so the
    /// Coach tab's first frame has cards before any network call starts — the fix for
    /// a tab that used to open blank.
    func cachedCoachFeed() -> CoachFeed? {
        useSampleData ? nil : DiskCache.load(CoachFeed.self, as: "coach_feed")
    }

    func cachedCoachThread(id: String) -> CoachThread? {
        useSampleData ? nil : DiskCache.load(CoachThread.self, as: "coach_thread_\(id)")
    }

    func cachedCoachHistory() -> CoachHistory? {
        useSampleData ? nil : DiskCache.load(CoachHistory.self, as: "coach_history")
    }

    func cachedCoachReports(period: String) -> CoachReportsList? {
        useSampleData ? nil : DiskCache.load(CoachReportsList.self,
                                             as: "coach_reports_\(period)")
    }

    func cachedCoachMemory() -> CoachMemory? {
        useSampleData ? nil : DiskCache.load(CoachMemory.self, as: "coach_memory")
    }

    // MARK: - Internals

    /// Retry pacing for a single logical request. The Cloud Run backend scales to
    /// zero when idle, so the first hit after a while cold-starts the instance and
    /// intermittently answers with a 503 while it does — riding through that here
    /// with a couple of quick, silent retries means the caller never has to
    /// manually tap "tentar de novo" three times to get in.
    private static let retryDelaysNs: [UInt64] = [0, 700_000_000, 1_800_000_000]

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem]) async throws -> T {
        var url = Config.baseURL.appending(path: path)
        if !query.isEmpty { url.append(queryItems: query) }

        var request = URLRequest(url: url)
        request.setValue(Config.authToken, forHTTPHeaderField: "X-Auth-Token")
        request.cachePolicy = .reloadIgnoringLocalCacheData  // always the live sheet
        return try await send(request, cacheAs: path)
    }

    /// As `get`, but caches under an explicit slash-free name — for a path like
    /// "insights/weekly" whose slash would otherwise become a bad file path on disk.
    private func get<T: Decodable>(_ path: String, query: [URLQueryItem],
                                   cacheAs: String) async throws -> T {
        var url = Config.baseURL.appending(path: path)
        if !query.isEmpty { url.append(queryItems: query) }
        var request = URLRequest(url: url)
        request.setValue(Config.authToken, forHTTPHeaderField: "X-Auth-Token")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        return try await send(request, cacheAs: cacheAs)
    }

    /// A mutation (/meals/edit, and the coach's refresh + chat).
    ///
    /// Retried the same way as `get`, which is only sound because every one of these
    /// is an absolute overwrite (/meals/edit sets fields to given values), a no-op
    /// when repeated (/coach/refresh dedups by slot), or explicitly keyed by an
    /// idempotency token the caller supplies (/coach/chat's `client_turn_id`).
    ///
    /// That last one is not decoration. This retry loop is silent and invisible, so
    /// a POST that is not idempotent gets applied two or three times with nobody
    /// noticing — which is exactly what happened to chat: three copies of one
    /// question, three different answers, all written to history. Anything added
    /// here must be safe to run twice, or carry a key that makes it so.
    private func post<T: Decodable>(_ path: String, body: [String: Any],
                                    timeout: TimeInterval? = nil) async throws -> T {
        var request = URLRequest(url: Config.baseURL.appending(path: path))
        request.httpMethod = "POST"
        request.setValue(Config.authToken, forHTTPHeaderField: "X-Auth-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        if let timeout { request.timeoutInterval = timeout }
        return try await send(request, cacheAs: nil)
    }

    /// Shared retry/decode loop for both GET and POST. `cacheAs` saves the raw
    /// response to disk under that name (GET only — nothing here should ever be
    /// read back as a POST's cached response).
    private func send<T: Decodable>(_ request: URLRequest, cacheAs: String?) async throws -> T {
        var lastError: Error = APIError.notHTTP
        for (attempt, delay) in Self.retryDelaysNs.enumerated() {
            let isLastAttempt = attempt == Self.retryDelaysNs.count - 1
            if delay > 0 { try? await Task.sleep(nanoseconds: delay) }
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                guard let http = response as? HTTPURLResponse else { throw APIError.notHTTP }
                guard http.statusCode == 200 else {
                    lastError = APIError.badStatus(http.statusCode)
                    if Self.isRetryableStatus(http.statusCode), !isLastAttempt { continue }
                    throw lastError
                }
                let decoded = try JSONDecoder().decode(T.self, from: data)
                if let cacheAs { DiskCache.save(data, as: cacheAs) }
                return decoded
            } catch {
                lastError = error
                if Self.isRetryableNetworkError(error), !isLastAttempt { continue }
                throw error
            }
        }
        throw lastError
    }

    private static func isRetryableStatus(_ code: Int) -> Bool {
        code == 503 || code == 502 || code == 504
    }

    private static func isRetryableNetworkError(_ error: Error) -> Bool {
        guard let urlError = error as? URLError else { return false }
        return [.timedOut, .networkConnectionLost, .cannotConnectToHost, .dnsLookupFailed]
            .contains(urlError.code)
    }

    /// DEBUG-only: render the whole app against bundled sample data by launching
    /// with USE_SAMPLE_DATA=1 (e.g. `SIMCTL_CHILD_USE_SAMPLE_DATA=1 simctl launch`),
    /// so the UI can be verified without a deployed backend. Never true in release.
    private var useSampleData: Bool {
        #if DEBUG
        return ProcessInfo.processInfo.environment["USE_SAMPLE_DATA"] == "1"
        #else
        return false
        #endif
    }
}
