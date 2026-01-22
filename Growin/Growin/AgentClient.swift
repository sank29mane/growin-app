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

class AgentClient: ObservableObject {
    @Published var isLoading = false
    @Published var lastAnswer: String = ""
    @Published var errorMsg: String?
    
    private let baseURL = "http://127.0.0.1:8002"
    
    func analyzePortfolio(query: String) async {
        DispatchQueue.main.async {
            self.isLoading = true
            self.errorMsg = nil
        }
        
        let url = URL(string: "\(baseURL)/agent/analyze")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body: [String: Any] = [
            "query": query,
            "model_name": "mistral" // Default to local model
        ]
        
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await URLSession.shared.data(for: request)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            let decoded = try JSONDecoder().decode(AgentResponse.self, from: data)
            
            DispatchQueue.main.async {
                self.lastAnswer = decoded.final_answer
                self.isLoading = false
            }
            
        } catch {
            DispatchQueue.main.async {
                self.errorMsg = error.localizedDescription
                self.isLoading = false
            }
        }
    }
}
