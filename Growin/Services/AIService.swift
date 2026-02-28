import Foundation

@MainActor
class AIService {
    let baseURL = AppConfig.shared.baseURL
    
    func streamStrategyEvents(ticker: String?) async throws -> AsyncThrowingStream<AgentEvent, Error> {
        var components = URLComponents(string: "\(baseURL)/api/ai/strategy/stream")!
        components.queryItems = [
            URLQueryItem(name: "session_id", value: UUID().uuidString),
            URLQueryItem(name: "ticker", value: ticker)
        ]
        
        let url = components.url!
        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw NSError(domain: "AIService", code: 0, userInfo: [NSLocalizedDescriptionKey: "Failed to connect to strategy stream"])
        }
        
        return AsyncThrowingStream { continuation in
            let task = Task {
                var currentEvent: String?
                
                do {
                    for try await line in bytes.lines {
                        if line.isEmpty {
                            currentEvent = nil
                            continue
                        }
                        
                        let parsed = SSEParser.parseLine(line)
                        if let event = parsed.event {
                            currentEvent = event
                        } else if let data = parsed.data {
                            if let eventName = currentEvent, let eventData = data.data(using: .utf8) {
                                let decoder = JSONDecoder()
                                decoder.keyDecodingStrategy = .useDefaultKeys
                                
                                switch eventName {
                                case "status_update", "reasoning_step":
                                    if let agentEvent = try? decoder.decode(AgentEvent.self, from: eventData) {
                                        continuation.yield(agentEvent)
                                    }
                                case "final_result":
                                    // Handle final result separately or yield as a special AgentEvent
                                    if let agentEvent = try? decoder.decode(AgentEvent.self, from: eventData) {
                                        continuation.yield(agentEvent)
                                    }
                                case "error":
                                    continuation.finish(throwing: NSError(domain: "AIService", code: 1, userInfo: [NSLocalizedDescriptionKey: data]))
                                default:
                                    break
                                }
                            }
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }
    
    func fetchStrategy(id: String) async throws -> AIStrategy {
        let url = URL(string: "\(baseURL)/api/ai/strategy/\(id)")!
        let (data, _) = try await URLSession.shared.data(from: url)
        
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .useDefaultKeys
        return try decoder.decode(AIStrategy.self, from: data)
    }
    
    func challengeStrategy(id: String, challenge: String) async throws -> (newSessionId: String, message: String) {
        var components = URLComponents(string: "\(baseURL)/api/ai/strategy/\(id)/challenge")!
        components.queryItems = [URLQueryItem(name: "challenge", value: challenge)]
        
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        
        let (data, _) = try await URLSession.shared.data(for: request)
        let response = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        
        return (
            response?["new_session_id"] as? String ?? "",
            response?["message"] as? String ?? ""
        )
    }
}
