import SwiftUI
import Combine

class GoalPlannerViewModel: ObservableObject {
    @Published var capital: Double = 5000
    @Published var targetReturn: Double = 15
    @Published var durationYears: Double = 5
    @Published var selectedRisk: String = "MEDIUM"
    
    @Published var plan: GoalPlan?
    @Published var isLoading = false
    @Published var errorMsg: String?
    @Published var executionSuccess = false
    @Published var showExecutionConfirmation = false
    
    private let client = MarketClient()
    
    let riskOptions = ["LOW", "MEDIUM", "HIGH", "AGGRESSIVE_PLUS"]
    
    func generatePlan() async {
        await MainActor.run {
            self.isLoading = true
            self.errorMsg = nil
            self.plan = nil
        }
        
        let result = await client.createGoalPlan(
            capital: capital,
            targetReturn: targetReturn,
            years: durationYears,
            risk: selectedRisk
        )
        
        await MainActor.run {
            self.plan = result
            self.isLoading = false
            if result == nil && self.errorMsg == nil {
                self.errorMsg = "Failed to generate plan. Please check your connection."
            }
        }
    }
    
    func executePlan() async {
        guard let plan = self.plan else { return }
        
        await MainActor.run {
            self.isLoading = true
            self.errorMsg = nil
        }
        
        let success = await client.executeGoalPlan(plan: plan)
        
        await MainActor.run {
            self.executionSuccess = success
            self.isLoading = false
            if success {
                self.showExecutionConfirmation = true
            } else {
                self.errorMsg = "Execution failed. Trading 212 may be disconnected."
            }
        }
    }
    
    func riskDescription(for risk: String) -> String {
        switch risk {
        case "LOW": return "Capital preservation focus"
        case "MEDIUM": return "Balanced growth & safety"
        case "HIGH": return "Equity-heavy growth"
        case "AGGRESSIVE_PLUS": return "Momentum-driven moonshots"
        default: return ""
        }
    }
    
    func riskIcon(for risk: String) -> String {
        switch risk {
        case "LOW": return "shield.safari.fill"
        case "MEDIUM": return "leaf.fill"
        case "HIGH": return "flame.fill"
        case "AGGRESSIVE_PLUS": return "rocket.fill"
        default: return "questionmark"
        }
    }
}
