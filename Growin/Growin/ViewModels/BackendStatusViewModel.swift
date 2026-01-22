import Foundation
import Combine

class BackendStatusViewModel: ObservableObject {
    @Published var isOnline: Bool = false
    @Published var lastCheck: Date?
    @Published var fullStatus: SystemStatusResponse?
    
    private var cancellables = Set<AnyCancellable>()
    private let baseURL = "http://127.0.0.1:8002"
    
    static let shared = BackendStatusViewModel()
    
    private init() {
        startPolling()
    }
    
    func startPolling() {
        Timer.publish(every: 5.0, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in
                self?.refreshStatus()
            }
            .store(in: &cancellables)
        
        refreshStatus()
    }
    
    func refreshStatus() {
        guard let url = URL(string: "\(baseURL)/api/system/status") else { return }
        
        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            DispatchQueue.main.async {
                if let data = data, let decoded = try? JSONDecoder().decode(SystemStatusResponse.self, from: data) {
                    self?.fullStatus = decoded
                    self?.isOnline = true
                } else {
                    self?.isOnline = false
                }
                self?.lastCheck = Date()
            }
        }.resume()
    }
    
    func checkHealth() {
        // Legacy method if needed, but refreshStatus replace it
        refreshStatus()
    }

    func launchBackend() {
        #if os(macOS)
        let projectPath = "/Users/sanketmane/Codes/Growin App"
        let script = """
        tell application "Terminal"
            activate
            do script "cd '\(projectPath)' && ./start_backend.sh"
        end tell
        """
        if let appleScript = NSAppleScript(source: script) {
            var error: NSDictionary?
            appleScript.executeAndReturnError(&error)
        }
        #endif
    }
}
