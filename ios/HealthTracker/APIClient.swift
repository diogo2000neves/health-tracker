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

    // MARK: - Internals

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem]) async throws -> T {
        var url = Config.baseURL.appending(path: path)
        if !query.isEmpty { url.append(queryItems: query) }

        var request = URLRequest(url: url)
        request.setValue(Config.authToken, forHTTPHeaderField: "X-Auth-Token")
        request.cachePolicy = .reloadIgnoringLocalCacheData  // always the live sheet

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw APIError.notHTTP }
        guard http.statusCode == 200 else { throw APIError.badStatus(http.statusCode) }

        return try JSONDecoder().decode(T.self, from: data)
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
