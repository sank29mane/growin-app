@preconcurrency import Foundation

// MARK: - Data Service Actor
/// Dedicated actor for background data processing to keep Main Thread free for 120Hz UI
actor PortfolioDataService {
    private let session = URLSession.shared
    private let baseURL: String
    
    init(baseURL: String = AppConfig.shared.baseURL) {
        self.baseURL = baseURL
    }
    
    // MARK: - Public API
    
    nonisolated func fetchPortfolio(accountType: String) async throws -> PortfolioSnapshot {
        try await get(endpoint: "/portfolio/live", query: [("account_type", accountType)])
    }
    
    nonisolated func fetchHistory(days: Int, accountType: String) async throws -> [PortfolioHistoryPoint] {
        try await get(endpoint: "/portfolio/history", query: [
            ("days", String(days)),
            ("account_type", accountType)
        ])
    }
    
    func switchAccountConfig(config: TradingConfig) async throws {
        try await post(endpoint: "/mcp/trading212/config", body: config)
    }
    
    func syncAccount(accountType: String) async throws {
        try await post(endpoint: "/account/active", body: ["account_type": accountType])
    }
    
    // MARK: - Private Helpers
    
    private func get<T: Decodable>(endpoint: String, query: [(String, String)] = []) async throws -> T {
        var components = URLComponents(string: baseURL + endpoint)
        components?.queryItems = query.map { URLQueryItem(name: $0.0, value: $0.1) }
        
        guard let url = components?.url else { throw URLError(.badURL) }
        
        let (data, response) = try await session.data(from: url)
        
        guard let httpResponse = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        
        if !(200...299).contains(httpResponse.statusCode) {
            // Try to parse backend error message
            if let errorJson = try? JSONDecoder().decode(BackendError.self, from: data) {
                 throw errorJson
            }
            // Fallback for known status codes
            if httpResponse.statusCode == 401 { throw BackendError(detail: "Unauthorized: Please check your API keys") }
            if httpResponse.statusCode == 503 { throw BackendError(detail: "Service Unavailable: T212 connection failed") }
            
            throw URLError(.badServerResponse)
        }
        
        return try JSONDecoder().decode(T.self, from: data)
    }
    
    private func post<B: Encodable>(endpoint: String, body: B) async throws {
        guard let url = URL(string: baseURL + endpoint) else { throw URLError(.badURL) }
        
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        
        let (data, response) = try await session.data(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        
        if !(200...299).contains(httpResponse.statusCode) {
             if let errorJson = try? JSONDecoder().decode(BackendError.self, from: data) {
                 throw errorJson
             }
             throw URLError(.badServerResponse)
        }
    }
}

// MARK: - Error Handling
struct BackendError: Error, Decodable, LocalizedError {
    let detail: String
    
    var errorDescription: String? {
        return detail
    }
}
