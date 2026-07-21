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
                DiskCache.save(data, as: path)
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
