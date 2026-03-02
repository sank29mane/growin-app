import SwiftUI
import Combine

/// Metrics from the CB-APM consensus models.
struct ConsensusMetrics: Codable, Sendable {
    let ttmR2: Double        // IBM Granite TTM-R2 forecast
    let xgboost: Double      // XGBoost technical validation
    let monteCarlo: Double   // Monte Carlo probability
    let combined: Double     // Final weighted consensus
}

/// A point in the passive income timeline.
struct IncomePoint: Identifiable, Equatable, Sendable {
    let id = UUID()
    let date: Date
    let amount: Double
    let isSettled: Bool // Solid Emerald vs Soft Blue
}

/// Forecast probability cloud ranges.
struct ProbabilityRange: Identifiable, Equatable, Sendable {
    let id = UUID()
    let date: Date
    let upper: Double
    let lower: Double
}

/// A manual HITL action requiring approval.
struct HITLAction: Identifiable, Equatable, Sendable {
    let id = UUID()
    let ticker: String
    let action: String    // "Buy", "Sell", "Abort"
    let reason: String    // "Confidence < 40%", "Max Drawdown > 5%"
    let confidence: Double
    let timestamp: Date
}

@MainActor
class PassiveIncomeViewModel: ObservableObject {
    @Published var consensus: ConsensusMetrics = ConsensusMetrics(ttmR2: 0.82, xgboost: 0.75, monteCarlo: 0.88, combined: 0.81)
    @Published var incomePoints: [IncomePoint] = []
    @Published var probabilityCloud: [ProbabilityRange] = []
    @Published var hitlActions: [HITLAction] = []
    
    @Published var totalSettled: Double = 0.0
    @Published var totalPredicted: Double = 0.0
    
    private var cancellables = Set<AnyCancellable>()
    
    init() {
        loadMockData()
    }
    
    func approveAction(_ action: HITLAction) {
        print("✅ Approved: \(action.ticker) - \(action.action)")
        hitlActions.removeAll { $0.id == action.id }
        // Future: Backend call
    }
    
    func abortAction(_ action: HITLAction) {
        print("🛑 Aborted: \(action.ticker) - \(action.action)")
        hitlActions.removeAll { $0.id == action.id }
        // Future: Backend call
    }
    
    private func loadMockData() {
        let calendar = Calendar.current
        let today = Date()
        
        // Mock Income Points
        var mockPoints: [IncomePoint] = []
        var settledTotal = 0.0
        var predictedTotal = 0.0
        
        // Past 6 months (Settled)
        for i in (1...6).reversed() {
            if let date = calendar.date(byAdding: .month, value: -i, to: today) {
                let amount = Double.random(in: 450...600)
                mockPoints.append(IncomePoint(date: date, amount: amount, isSettled: true))
                settledTotal += amount
            }
        }
        
        // Future 6 months (Predicted)
        for i in 0...6 {
            if let date = calendar.date(byAdding: .month, value: i, to: today) {
                let amount = Double.random(in: 550...750)
                mockPoints.append(IncomePoint(date: date, amount: amount, isSettled: i == 0 ? false : false)) // Today is technically not yet settled
                predictedTotal += amount
            }
        }
        
        self.incomePoints = mockPoints
        self.totalSettled = settledTotal
        self.totalPredicted = predictedTotal
        
        // Mock Probability Cloud
        var mockCloud: [ProbabilityRange] = []
        for i in 0...120 { // Next 120 days
            if let date = calendar.date(byAdding: .day, value: i, to: today) {
                let base = 100.0 + Double(i) * 0.1
                let spread = Double(i) * 0.15 + 5.0
                mockCloud.append(ProbabilityRange(date: date, upper: base + spread, lower: base - spread))
            }
        }
        self.probabilityCloud = mockCloud
        
        // Mock HITL Actions
        self.hitlActions = [
            HITLAction(
                ticker: "AAPL",
                action: "Dividend Capture Entry",
                reason: "TTM-R2 & XGBoost high confidence (81%)",
                confidence: 0.81,
                timestamp: today
            ),
            HITLAction(
                ticker: "TSLA",
                action: "Abort / Exit Position",
                reason: "Max Drawdown threshold breached (>5%)",
                confidence: 0.95,
                timestamp: today
            )
        ]
    }
}
