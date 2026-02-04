import Foundation
import Combine

@Observable @MainActor
class BackendStatusViewModel {
    var isOnline: Bool = false
    var lastCheck: Date?
    var fullStatus: SystemStatusResponse?
    
    private let config = AppConfig.shared
    private var pollTask: Task<Void, Never>?
    
    static let shared = BackendStatusViewModel()
    
    private init() {
        startPolling()
    }
    
    func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await refreshStatus()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }
    
    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
    
    func refreshStatus() async {
        guard let url = URL(string: "\(config.baseURL)/api/system/status") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let decoded = try? JSONDecoder().decode(SystemStatusResponse.self, from: data) {
                self.fullStatus = decoded
                self.isOnline = true
            } else {
                self.isOnline = false
            }
        } catch {
            self.isOnline = false
        }
        self.lastCheck = Date()
    }
    
    func checkHealth() {
        Task { await refreshStatus() }
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
