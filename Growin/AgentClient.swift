import Foundation
import Combine

enum SSEEvent: String {
    case token, status, meta, done, error
}

enum AgentStreamEvent: Sendable {
    case token(String)
    case status(String)
    case meta([String: AnySendable])
    case error(String)
    case done
}

struct AgentResponse: Codable, Sendable {
    let messages: [[String: AnySendable]]
    let finalAnswer: String
    
    enum CodingKeys: String, CodingKey {
        case messages
        case finalAnswer = "final_answer"
    }
}

struct AnySendable: @unchecked Sendable, Codable {
    let value: Any
    
    init(_ value: Any) {
        self.value = value
    }
    
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let dict = try? container.decode([String: AnyCodable].self) {
            self.value = dict.mapValues { $0.value }
        } else if let array = try? container.decode([AnyCodable].self) {
            self.value = array.map { $0.value }
        } else if let string = try? container.decode(String.self) {
            self.value = string
        } else if let int = try? container.decode(Int.self) {
            self.value = int
        } else if let double = try? container.decode(Double.self) {
            self.value = double
        } else if let bool = try? container.decode(Bool.self) {
            self.value = bool
        } else {
            self.value = ""
        }
    }
    
    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        if let dict = value as? [String: Any] {
            try container.encode(dict.mapValues { AnyCodable($0) })
        } else if let array = value as? [Any] {
            try container.encode(array.map { AnyCodable($0) })
        } else if let string = value as? String {
            try container.encode(string)
        } else if let int = value as? Int {
            try container.encode(int)
        } else if let double = value as? Double {
            try container.encode(double)
        } else if let bool = value as? Bool {
            try container.encode(bool)
        }
    }
}

struct AgentClient {
    private let config = AppConfig.shared
    
    /// SOTA: Implementing robust SSE streaming with AsyncStream
    func streamMessage(query: String, conversationId: String? = nil, model: String? = nil) -> AsyncStream<AgentStreamEvent> {
        AsyncStream { continuation in
            let url = URL(string: "\(config.baseURL)/api/chat/message")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            
            // Context from UserDefaults
            let defaults = UserDefaults.standard
            let body: [String: Any] = [
                "message": query,
                "conversation_id": conversationId as Any,
                "model_name": model ?? "native-mlx",
                "coordinator_model": defaults.string(forKey: "selectedCoordinatorModel") ?? "granite-tiny",
                "account_type": defaults.string(forKey: "t212AccountType") ?? "invest"
            ]
            
            do {
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
                
                let task = Task {
                    do {
                        let (result, response) = try await URLSession.shared.bytes(for: request)
                        
                        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                            continuation.yield(.error("Server error: \((response as? HTTPURLResponse)?.statusCode ?? 0)"))
                            continuation.finish()
                            return
                        }
                        
                        var currentEvent: String?
                        
                        for try await line in result.lines {
                            // Check for task cancellation
                            if Task.isCancelled {
                                continuation.finish()
                                return
                            }
                            
                            let trimmedLine = line.trimmingCharacters(in: .whitespaces)
                            if trimmedLine.isEmpty { continue }
                            
                            if trimmedLine.hasPrefix("event: ") {
                                currentEvent = String(trimmedLine.dropFirst(7))
                            } else if trimmedLine.hasPrefix("data: ") {
                                let data = String(trimmedLine.dropFirst(6))
                                
                                if data == "[DONE]" {
                                    continuation.yield(.done)
                                    break
                                }
                                
                                handleEvent(currentEvent ?? "token", data: data, into: continuation)
                            }
                        }
                        continuation.finish()
                    } catch {
                        continuation.yield(.error(error.localizedDescription))
                        continuation.finish()
                    }
                }
                
                continuation.onTermination = { @Sendable _ in
                    task.cancel()
                }
                
            } catch {
                continuation.yield(.error(error.localizedDescription))
                continuation.finish()
            }
        }
    }
    
    private func handleEvent(_ event: String, data: String, into continuation: AsyncStream<AgentStreamEvent>.Continuation) {
        switch event {
        case "token":
            continuation.yield(.token(data))
        case "status":
            continuation.yield(.status(data))
        case "meta":
            if let jsonData = data.data(using: .utf8),
               let metaDict = try? JSONDecoder().decode([String: AnySendable].self, from: jsonData) {
                continuation.yield(.meta(metaDict))
            }
        case "error":
            continuation.yield(.error(data))
        default:
            // Default to token if event is unspecified (SOTA 2026 standard)
            continuation.yield(.token(data))
        }
    }
    
    func analyzePortfolio(query: String) async -> AgentResponse? {
        let url = URL(string: "\(config.baseURL)/agent/analyze")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body: [String: Any] = [
            "query": query,
            "model_name": "native-mlx"
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
