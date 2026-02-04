import SwiftUI
import Combine

@Observable @MainActor
class GoalPlannerViewModel {
    var capital: Double = 5000
    var targetReturn: Double = 15
    var durationYears: Double = 5
    var selectedRisk: String = "MEDIUM"
    
    var plan: GoalPlan?
    var isLoading = false
    var errorMsg: String?
    var executionSuccess = false
    var showExecutionConfirmation = false
    
    private let client = MarketClient()
    
    let riskOptions = ["LOW", "MEDIUM", "HIGH", "AGGRESSIVE_PLUS"]
    
    func generatePlan() async {
        self.isLoading = true
        self.errorMsg = nil
        self.plan = nil
        
        let result = await client.createGoalPlan(
            capital: capital,
            targetReturn: targetReturn,
            years: durationYears,
            risk: selectedRisk
        )
        
        self.plan = result
        self.isLoading = false
        if result == nil && self.errorMsg == nil {
            self.errorMsg = "Failed to generate plan. Please check your connection."
        }
    }
    
    func executePlan() async {
        guard let plan = self.plan else { return }
        
        self.isLoading = true
        self.errorMsg = nil
        
        let success = await client.executeGoalPlan(plan: plan)
        
        self.executionSuccess = success
        self.isLoading = false
        if success {
            self.showExecutionConfirmation = true
        } else {
            self.errorMsg = "Execution failed. Trading 212 may be disconnected."
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
