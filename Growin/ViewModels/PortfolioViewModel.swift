import SwiftUI
import Combine

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
        
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.fetchPortfolio() }
            group.addTask { await self.fetchHistory() }
        }
        
        isLoading = false
    }
    
    func switchAccount(newType: String) async {
        defaults.set(newType, forKey: "t212AccountType")
        isSwitchingAccount = true
        errorMsg = nil
        
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
            self.errorMsg = "Failed to switch account: \(error.localizedDescription)"
        }
        
        isSwitchingAccount = false
    }
    
    func fetchPortfolio() async {
        do {
            let result = try await dataService.fetchPortfolio(accountType: accountType)
            withAnimation(.spring(response: 0.5, dampingFraction: 0.7)) {
                self.snapshot = result
                self.lastUpdated = Date()
            }
        } catch {
            self.errorMsg = "Portfolio Error: \(error.localizedDescription)"
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
        }
    }
}
