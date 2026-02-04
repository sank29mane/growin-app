import SwiftUI
import Combine

@Observable @MainActor
class PortfolioSummaryObserver {
    static let shared = PortfolioSummaryObserver()
    
    var lastSummary: PortfolioSummary?
    var menuBarLabel: String = "Growin"
    var menuBarIcon: String = "chart.line.uptrend.xyaxis"
    
    private var pollTask: Task<Void, Never>?
    
    private init() {
        startPolling()
    }
    
    func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await fetchSummary()
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }
    
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
    
    func fetchSummary() async {
        guard let url = URL(string: "\(AppConfig.shared.baseURL)/portfolio/live") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoded = try JSONDecoder().decode(PortfolioSnapshot.self, from: data)
            
            self.lastSummary = decoded.summary
            
            // Update Menu Bar Label
            if let pnl = decoded.summary?.totalPnl {
                let sign = pnl >= 0 ? "+" : ""
                self.menuBarLabel = "G: \(sign)£\(String(format: "%.0f", pnl))"
                self.menuBarIcon = pnl >= 0 ? "chart.line.uptrend.xyaxis" : "chart.line.downtrend.xyaxis"
            }
        } catch {
            print("❌ Summary observer fetch error: \(error)")
        }
    }
}
