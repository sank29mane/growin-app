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
    
    func streamMessage(query: String, conversationId: String? = nil, model: String? = nil) -> AsyncStream<AgentStreamEvent> {
        AsyncStream { continuation in
            let url = URL(string: "\(config.baseURL)/api/chat/message")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
            
            let body: [String: Any] = [
                "message": query,
                "conversation_id": conversationId as Any,
                "model_name": model ?? "native-mlx"
            ]
            
            do {
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
                
                Task {
                    do {
                        let (result, _) = try await URLSession.shared.bytes(for: request)
                        
                        for try await line in result.lines {
                            guard line.hasPrefix("data: ") else { continue }
                            let data = String(line.dropFirst(6))
                            
                            if data == "[DONE]" {
                                continuation.yield(.done)
                                break
                            }
                            
                            // Simple parsing for this pass
                            if let jsonData = data.data(using: .utf8) {
                                if let dict = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                                    if let event = dict["event"] as? String {
                                        let payload = dict["data"] as? String ?? ""
                                        
                                        switch event {
                                        case "token": continuation.yield(.token(payload))
                                        case "status": continuation.yield(.status(payload))
                                        case "meta": 
                                            if let metaData = payload.data(using: .utf8),
                                               let metaDict = try? JSONDecoder().decode([String: AnySendable].self, from: metaData) {
                                                continuation.yield(.meta(metaDict))
                                            }
                                        case "error": continuation.yield(.error(payload))
                                        default: break
                                        }
                                    }
                                }
                            }
                        }
                        continuation.finish()
                    } catch {
                        continuation.yield(.error(error.localizedDescription))
                        continuation.finish()
                    }
                }
            } catch {
                continuation.yield(.error(error.localizedDescription))
                continuation.finish()
            }
        }
    }
    
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
