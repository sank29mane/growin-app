import SwiftUI
import Combine

class PortfolioSummaryObserver: ObservableObject {
    static let shared = PortfolioSummaryObserver()
    
    @Published var lastSummary: PortfolioSummary?
    @Published var menuBarLabel: String = "Growin"
    @Published var menuBarIcon: String = "chart.line.uptrend.xyaxis"
    private var timer: AnyCancellable?
    
    private init() {
        startPolling()
    }
    
    func startPolling() {
        // Poll every 60 seconds
        timer = Timer.publish(every: 60, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in
                Task {
                    await self?.fetchSummary()
                }
            }
        
        // Initial fetch
        Task {
            await fetchSummary()
        }
    }
    
    func fetchSummary() async {
        // We use the same endpoint as PortfolioView but maybe a lighter one if it existed.
        // For now, we reuse the live portfolio endpoint.
        guard let url = URL(string: "http://127.0.0.1:8002/portfolio/live") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoded = try JSONDecoder().decode(PortfolioSnapshot.self, from: data)
            
            await MainActor.run {
                self.lastSummary = decoded.summary
                
                // Update Menu Bar Label
                if let pnl = decoded.summary?.totalPnl {
                    let sign = pnl >= 0 ? "+" : ""
                    self.menuBarLabel = "G: \(sign)£\(String(format: "%.2f", pnl))"
                    self.menuBarIcon = pnl >= 0 ? "chart.line.uptrend.xyaxis" : "chart.line.downtrend.xyaxis"
                }
            }
        } catch {
            print("❌ Summary observer fetch error: \(error)")
        }
    }
}
