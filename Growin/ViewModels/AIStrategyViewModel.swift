import Foundation
import SwiftUI

@Observable @MainActor
class AIStrategyViewModel {
    var strategy: AIStrategy?
    var streamingEvents: [AgentEvent] = []
    var isStreaming = false
    var errorMessage: String?
    
    // Optimistic UI State
    var optimisticStatus: String?
    private var rollbackStrategy: AIStrategy?
    
    private let aiService = AIService()
    
    func generateStrategy(ticker: String? = nil) async {
        isStreaming = true
        errorMessage = nil
        streamingEvents = []
        strategy = nil
        
        do {
            let stream = try await aiService.streamStrategyEvents(ticker: ticker)
            
            for try await event in stream {
                let currentEvent = event // Capture for task
                Task { @MainActor in
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                        streamingEvents.append(currentEvent)
                    }
                }
            }
            
            isStreaming = false
        } catch {
            errorMessage = error.localizedDescription
            isStreaming = false
        }
    }
    
    func challengeStrategy(challenge: String) async {
        guard let currentId = strategy?.strategyId else { return }
        
        // Optimistic UI: Immediately show "Re-stitching..." status
        optimisticStatus = "Re-stitching Strategy Trajectories..."
        rollbackStrategy = strategy
        strategy = nil
        
        do {
            let result = try await aiService.challengeStrategy(id: currentId, challenge: challenge)
            optimisticStatus = result.message
            
            // Trigger new generation
            await generateStrategy()
            
            optimisticStatus = nil
        } catch {
            // Graceful Rollback
            strategy = rollbackStrategy
            errorMessage = "Challenge failed: \(error.localizedDescription)"
            optimisticStatus = nil
        }
    }
}
