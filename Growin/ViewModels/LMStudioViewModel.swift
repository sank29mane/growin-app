import Foundation
import Combine
import SwiftUI

@Observable @MainActor
class LMStudioViewModel {
    var availableModels: [String] = []
    var isFetchingModels: Bool = false
    var isLoadingModel: Bool = false
    var loadingStatus: String = "Idle"
    var currentModel: String?
    var isOnline: Bool = false
    
    // SOTA: State Machine for Model Switching
    var requestedModelId: String?
    private var lastLoadTriggered: Date?
    
    var isLoaded: Bool {
        guard let current = currentModel, !current.isEmpty else { return false }
        if let requested = requestedModelId {
            return current == requested
        }
        return true
    }
    
    private let config = AppConfig.shared
    private var statusPollTask: Task<Void, Never>?
    
    static let shared = LMStudioViewModel()
    
    private init() {
        startStatusPolling()
    }
    
    func startStatusPolling() {
        statusPollTask?.cancel()
        statusPollTask = Task {
            while !Task.isCancelled {
                await refreshStatus()
                // Reduced frequency to 10s to minimize server log spam
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }
    
    private var isPollingStatus: Bool = false
    
    func refreshStatus() async {
        guard !isPollingStatus else { return }
        guard let url = URL(string: "\(config.baseURL)/api/models/lmstudio/status") else { return }
        
        isPollingStatus = true
        defer { isPollingStatus = false }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let response = try JSONDecoder().decode(LMStudioStatusResponse.self, from: data)
            
            self.isOnline = response.status == "online"
            self.currentModel = response.loadedModel
            
            // 1. Success condition: Target reached
            if let requested = requestedModelId, self.currentModel == requested {
                self.requestedModelId = nil
                self.isLoadingModel = false
                self.loadingStatus = "Ready"
                return
            }
            
            // 2. Loading State Logic
            let agentStatus = BackendStatusViewModel.shared.fullStatus?.agents["lmstudio"]
            
            if agentStatus?.status == "working" {
                self.isLoadingModel = true
                self.loadingStatus = agentStatus?.detail ?? "Loading..."
                // Extend shield while working
                lastLoadTriggered = Date() 
            } else if let lastTrigger = lastLoadTriggered, Date().timeIntervalSince(lastTrigger) < 30 {
                // Flicker Shield: 30s lock to allow LM Studio to allocate VRAM & Backend to update status
                self.isLoadingModel = true
                if self.loadingStatus == "Idle" || self.loadingStatus == "Ready" {
                    self.loadingStatus = "Initiating switch..."
                }
            } else {
                // Outside grace period
                if requestedModelId == nil {
                    self.isLoadingModel = false
                    self.loadingStatus = agentStatus?.status ?? "Ready"
                } else if self.currentModel != requestedModelId {
                    // We are still waiting for the backend to start the work (Outside 30s)
                    self.isLoadingModel = true
                    if self.loadingStatus == "Ready" || self.loadingStatus == "Initiating switch..." {
                        self.loadingStatus = "Switching..."
                    }
                    
                    // SOTA: Auto-clear if stuck for too long (e.g. 120s)
                    if let lastTrigger = lastLoadTriggered, Date().timeIntervalSince(lastTrigger) > 120 {
                         self.requestedModelId = nil
                         self.isLoadingModel = false
                         self.loadingStatus = "Switch timed out"
                    }
                }
            }
            
        } catch {
            self.isOnline = false
        }
    }
    
    func fetchModels() {
        guard !isFetchingModels else { return }
        isFetchingModels = true
        
        Task {
            guard let url = URL(string: "\(config.baseURL)/api/models/lmstudio") else { 
                isFetchingModels = false
                return 
            }
            
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                let response = try JSONDecoder().decode(LMStudioModelsResponse.self, from: data)
                self.availableModels = response.models
                self.isOnline = response.status == "online"
            } catch {
                print("Failed to fetch LM Studio models: \(error)")
                self.isOnline = false
            }
            isFetchingModels = false
        }
    }
    
    func loadModel(_ modelId: String) {
        // Prevent duplicate loads or re-loading the active model
        guard !isLoadingModel, currentModel != modelId else { return }
        
        isLoadingModel = true
        requestedModelId = modelId
        lastLoadTriggered = Date()
        loadingStatus = "Requesting \(modelId)..."
        
        Task {
            guard let url = URL(string: "\(config.baseURL)/api/models/lmstudio/load") else {
                isLoadingModel = false
                requestedModelId = nil
                return
            }
            
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            
            let payload = LMStudioLoadRequest(modelId: modelId, contextLength: 8192, gpuOffload: "max")
            request.httpBody = try? JSONEncoder().encode(payload)
            
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
                    if let errorJson = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let msg = errorJson["message"] as? String {
                        loadingStatus = "Error: \(msg)"
                    } else {
                        loadingStatus = "Load failed (HTTP \(httpResponse.statusCode))"
                    }
                    isLoadingModel = false
                    requestedModelId = nil
                } else if let result = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                   result["status"] == "success" {
                    loadingStatus = "Clearing VRAM & Loading..."
                } else {
                    loadingStatus = "Load failed"
                    isLoadingModel = false
                    requestedModelId = nil
                }
            } catch {
                print("Failed to load LM Studio model: \(error)")
                loadingStatus = "Error: \(error.localizedDescription)"
                isLoadingModel = false
                requestedModelId = nil
            }
        }
    }
}
