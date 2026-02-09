import Foundation
import Combine

struct AgentResponse: Codable {
    let messages: [Message]
    let final_answer: String
    
    enum CodingKeys: String, CodingKey {
        case messages
        case final_answer = "final_answer"
    }
}

// Flexible Message decoding
struct Message: Codable, Identifiable {
    let id = UUID()
    let type: String
    let content: String
    
    enum CodingKeys: String, CodingKey {
        case type
        case content
    }
    
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.type = try container.decode(String.self, forKey: .type)
        self.content = try container.decode(String.self, forKey: .content)
    }
}

struct AgentClient {
    private let config = AppConfig.shared
    
    func analyzePortfolio(query: String) async -> AgentResponse? {
        let url = URL(string: "\(config.baseURL)/agent/analyze")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body: [String: Any] = [
            "query": query,
            "model_name": "native-mlx" // SOTA 2026 default
        ]
        
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                return nil
            }
            
            return try JSONDecoder().decode(AgentResponse.self, from: data)
        } catch {
            print("AgentClient Error: \(error)")
            return nil
        }
    }
}
