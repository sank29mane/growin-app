import Foundation
import Combine

class MarketClient: ObservableObject {
    @Published var isLoading = false
    @Published var errorMsg: String?
    
    private let baseURL = "http://127.0.0.1:8002"
    
    func createGoalPlan(capital: Double, targetReturn: Double, years: Double, risk: String) async -> GoalPlan? {
        await MainActor.run {
            self.isLoading = true
            self.errorMsg = nil
        }
        
        let url = URL(string: "\(baseURL)/api/goal/plan")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body: [String: Any] = [
            "initial_capital": capital,
            "target_returns_percent": targetReturn,
            "duration_years": years,
            "risk_profile": risk
        ]
        
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            let decoded = try JSONDecoder().decode(GoalPlan.self, from: data)
            
            await MainActor.run {
                self.isLoading = false
            }
            return decoded
            
        } catch {
            await MainActor.run {
                self.errorMsg = error.localizedDescription
                self.isLoading = false
            }
            return nil
        }
    }
    
    func executeGoalPlan(plan: GoalPlan) async -> Bool {
        await MainActor.run {
            self.isLoading = true
            self.errorMsg = nil
        }
        
        let url = URL(string: "\(baseURL)/api/goal/execute")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        do {
            let body = try JSONEncoder().encode(plan)
            request.httpBody = body
            
            let (_, response) = try await URLSession.shared.data(for: request)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            await MainActor.run {
                self.isLoading = false
            }
            return true
            
        } catch {
            await MainActor.run {
                self.errorMsg = error.localizedDescription
                self.isLoading = false
            }
            return false
        }
    }
}
