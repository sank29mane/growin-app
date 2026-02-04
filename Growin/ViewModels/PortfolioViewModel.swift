import SwiftUI
import Combine


// MARK: - Main ViewModel
@Observable @MainActor
class PortfolioViewModel {
    var snapshot: PortfolioSnapshot?
    var isLoading: Bool = false
    var errorMsg: String?
    var lastUpdated: Date?
    var portfolioHistory: [PortfolioHistoryPoint] = []
    var selectedTimeRange: TimeRange = .week
    var selectedPosition: Position?
    var isSwitchingAccount = false
    
    private let dataService = PortfolioDataService()
    private let defaults = UserDefaults.standard
    
    // Settings getters/setters helper
    private var accountType: String {
        defaults.string(forKey: "t212AccountType") ?? "invest"
    }
    
    init() {}
    
    func onAppear() async {
        if snapshot == nil || portfolioHistory.isEmpty {
            await refreshAll()
        }
    }
    
    func refreshAll() async {
        isLoading = true
        errorMsg = nil
        defer { isLoading = false }
        
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.fetchPortfolio() }
            group.addTask { await self.fetchHistory() }
        }
    }
    
    func switchAccount(newType: String) async {
        defaults.set(newType, forKey: "t212AccountType")
        isSwitchingAccount = true
        errorMsg = nil
        defer { isSwitchingAccount = false }
        
        let config = TradingConfig(
            accountType: newType,
            investKey: defaults.string(forKey: "t212InvestKey") ?? "",
            investSecret: defaults.string(forKey: "t212InvestSecret") ?? "",
            isaKey: defaults.string(forKey: "t212IsaKey") ?? "",
            isaSecret: defaults.string(forKey: "t212IsaSecret") ?? ""
        )
        
        do {
            try await dataService.switchAccountConfig(config: config)
            try await dataService.syncAccount(accountType: newType)
            await refreshAll()
        } catch {
            print("Switch account error: \(error)")
            self.errorMsg = "Failed to switch account: \(error.localizedDescription)"
        }
    }
    
    func fetchPortfolio() async {
        do {
            let result = try await dataService.fetchPortfolio(accountType: accountType)
            withAnimation(.spring(response: 0.5, dampingFraction: 0.7)) {
                self.snapshot = result
                self.lastUpdated = Date()
                // Don't clear error if it was set by history fetch
            }
        } catch {
            self.errorMsg = "Portfolio Error: \(error.localizedDescription)"
            print("Portfolio fetch error: \(error)")
        }
    }
    
    func fetchHistory() async {
        let days = selectedTimeRange.days
        do {
            let result = try await dataService.fetchHistory(days: days, accountType: accountType)
            withAnimation(.easeInOut) {
                self.portfolioHistory = result
            }
        } catch {
            if self.errorMsg == nil {
                self.errorMsg = "History Error: \(error.localizedDescription)"
            }
            print("History fetch error: \(error)")
        }
    }
}
