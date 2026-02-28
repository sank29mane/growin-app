import Foundation

struct AppConfig: Sendable {
    nonisolated static let shared = AppConfig()
    
    // Base URL for the Python Backend
    // Using localhost for local development
    let baseURL: String = {
        // You could add logic here to load from Info.plist or config file
        return "http://127.0.0.1:8002"
    }()
    
    let webSocketURL: String = {
        return "ws://127.0.0.1:8002"
    }()
}
