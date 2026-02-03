import Foundation

struct AppConfig: Sendable {
    nonisolated static let shared = AppConfig()
    
    // Base URL for the Python Backend
    // Using localhost for simulator, but customizable for device
    let baseURL: String = {
        // You could add logic here to detect if running on simulator vs device
        // or load from Info.plist
        return "http://127.0.0.1:8002"
    }()
    
    let webSocketURL: String = {
        return "ws://127.0.0.1:8002"
    }()
}
