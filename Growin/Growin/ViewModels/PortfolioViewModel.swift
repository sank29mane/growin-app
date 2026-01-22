import SwiftUI
import Combine

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
    
    // Initializer
    init() {
        // Auto-fetch on init if needed, or wait for onAppear
    }
    
    func onAppear() async {
        if snapshot == nil {
            await fetchPortfolio()
            await fetchHistory()
        }
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
        
        guard let url = URL(string: "http://127.0.0.1:8002/mcp/trading212/config") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: config)
        
        do {
            let (_, _) = try await URLSession.shared.data(for: request)
            
            // Sync active state
            await syncAccountToBackend(t212AccountType)
            
            // Refresh data after switch
            await fetchPortfolio()
            await fetchHistory()
        } catch {
            print("Switch account error: \(error)")
        }
    }
    
    func fetchPortfolio() async {
        await MainActor.run { isLoading = true }
        defer { Task { await MainActor.run { isLoading = false } } }
        
        guard let url = URL(string: "http://127.0.0.1:8002/portfolio/live?account_type=\(t212AccountType)") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoded = try JSONDecoder().decode(PortfolioSnapshot.self, from: data)
            
            await MainActor.run {
                withAnimation(.spring(response: 0.5, dampingFraction: 0.7)) {
                    self.snapshot = decoded
                    self.lastUpdated = Date()
                    self.errorMsg = nil
                }
            }
        } catch {
            await MainActor.run {
                self.errorMsg = error.localizedDescription
                print("Portfolio fetch error: \(error)")
            }
        }
    }
    
    func fetchHistory() async {
        let days = selectedTimeRange.days
        guard let url = URL(string: "http://127.0.0.1:8002/portfolio/history?days=\(days)&account_type=\(t212AccountType)") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoded = try JSONDecoder().decode([PortfolioHistoryPoint].self, from: data)
            
            await MainActor.run {
                withAnimation(.easeInOut) {
                    self.portfolioHistory = decoded
                }
            }
        } catch {
            print("History fetch error: \(error)")
        }
    }
    
    func syncAccountToBackend(_ accountType: String) async {
        guard let url = URL(string: "http://127.0.0.1:8002/account/active") else { return }
        
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body = ["account_type": accountType]
        request.httpBody = try? JSONEncoder().encode(body)
        
        do {
            let (_, _) = try await URLSession.shared.data(for: request)
            print("✅ Synced active account to backend: \(accountType)")
        } catch {
            print("❌ Failed to sync account: \(error)")
        }
    }
}
