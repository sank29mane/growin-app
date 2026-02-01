import SwiftUI
import Combine

// MARK: - Data Service Actor
/// Dedicated actor for background data processing to keep Main Thread free for 120Hz UI
actor PortfolioDataService {
    private let session = URLSession.shared
    private let baseURL = "http://127.0.0.1:8002"
    
    func fetchPortfolio(accountType: String) async throws -> PortfolioSnapshot {
        guard let url = URL(string: "\(baseURL)/portfolio/live?account_type=\(accountType)") else {
            throw URLError(.badURL)
        }
        
        let (data, _) = try await session.data(from: url)
        return try JSONDecoder().decode(PortfolioSnapshot.self, from: data)
    }
    
    func fetchHistory(days: Int, accountType: String) async throws -> [PortfolioHistoryPoint] {
        guard let url = URL(string: "\(baseURL)/portfolio/history?days=\(days)&account_type=\(accountType)") else {
            throw URLError(.badURL)
        }
        
        let (data, _) = try await session.data(from: url)
        return try JSONDecoder().decode([PortfolioHistoryPoint].self, from: data)
    }
    
    func switchAccountConfig(config: [String: Any]) async throws {
        guard let url = URL(string: "\(baseURL)/mcp/trading212/config") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: config)
        let (_, _) = try await session.data(for: request)
    }
    
    func syncAccount(accountType: String) async throws {
        guard let url = URL(string: "\(baseURL)/account/active") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body = ["account_type": accountType]
        request.httpBody = try? JSONEncoder().encode(body)
        let (_, _) = try await session.data(for: request)
    }
}

// MARK: - Main ViewModel
@MainActor
class PortfolioViewModel: ObservableObject {
    @Published var snapshot: PortfolioSnapshot?
    @Published var isLoading: Bool = false
    @Published var errorMsg: String?
    @Published var lastUpdated: Date?
    @Published var portfolioHistory: [PortfolioHistoryPoint] = []
    @Published var selectedTimeRange: TimeRange = .week
    @Published var selectedPosition: Position?
    
    // Explicitly managed AppStorage/StandardUserDefaults content for ViewModel usage
    @AppStorage("t212InvestKey") var t212InvestKey = ""
    @AppStorage("t212InvestSecret") var t212InvestSecret = ""
    @AppStorage("t212IsaKey") var t212IsaKey = ""
    @AppStorage("t212IsaSecret") var t212IsaSecret = ""
    @AppStorage("t212AccountType") var t212AccountType = "invest"
    
    @Published var isSwitchingAccount = false
    
    private let dataService = PortfolioDataService()
    
    // Initializer
    init() {
        // Auto-fetch on init if needed, or wait for onAppear
    }
    
    func onAppear() async {
        // Only fetch if stale or empty
        if snapshot == nil {
            await refreshAll()
        }
    }
    
    func refreshAll() async {
        isLoading = true
        defer { isLoading = false }
        
        async let portfolio = fetchPortfolio()
        async let history = fetchHistory()
        
        _ = await (portfolio, history)
    }
    
    func switchAccount(newType: String) async {
        t212AccountType = newType
        isSwitchingAccount = true
        defer { isSwitchingAccount = false }
        
        let config: [String: Any] = [
            "account_type": t212AccountType,
            "invest_key": t212InvestKey,
            "invest_secret": t212InvestSecret,
            "isa_key": t212IsaKey,
            "isa_secret": t212IsaSecret
        ]
        
        do {
            try await dataService.switchAccountConfig(config: config)
            try await dataService.syncAccount(accountType: t212AccountType)
            await refreshAll()
        } catch {
            print("Switch account error: \(error)")
            self.errorMsg = "Failed to switch account"
        }
    }
    
    func fetchPortfolio() async {
        do {
            let result = try await dataService.fetchPortfolio(accountType: t212AccountType)
            withAnimation(.spring(response: 0.5, dampingFraction: 0.7)) {
                self.snapshot = result
                self.lastUpdated = Date()
                self.errorMsg = nil
            }
        } catch {
            self.errorMsg = error.localizedDescription
            print("Portfolio fetch error: \(error)")
        }
    }
    
    func fetchHistory() async {
        let days = selectedTimeRange.days
        do {
            let result = try await dataService.fetchHistory(days: days, accountType: t212AccountType)
            withAnimation(.easeInOut) {
                self.portfolioHistory = result
            }
        } catch {
            print("History fetch error: \(error)")
        }
    }
}
