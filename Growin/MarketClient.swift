import Foundation

struct MarketClient {
    private let config = AppConfig.shared
    
    func createGoalPlan(capital: Decimal, targetReturn: Decimal, years: Decimal, risk: String) async -> GoalPlan? {
        let url = URL(string: "\(config.baseURL)/api/goal/plan")!
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
                return nil
            }
            
            return try JSONDecoder().decode(GoalPlan.self, from: data)
        } catch {
            print("MarketClient Error: \(error)")
            return nil
        }
    }
    
    func executeGoalPlan(plan: GoalPlan) async -> Bool {
        let url = URL(string: "\(config.baseURL)/api/goal/execute")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        do {
            let body = try JSONEncoder().encode(plan)
            request.httpBody = body
            
            let (_, response) = try await URLSession.shared.data(for: request)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                return false
            }
            
            return true
        } catch {
            print("MarketClient Execution Error: \(error)")
            return false
        }
    }
}
