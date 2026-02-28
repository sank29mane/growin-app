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
                withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                    streamingEvents.append(event)
                    
                    if event.eventType == "final_result", let _ = event.step?.content {
                        // In my mock, strategy_id is passed in content or similar
                        // Let's assume for now we need a follow up fetch or the event has it
                    }
                    
                    // Specific handling for final_result if we want to fetch it immediately
                }
                
                // Hack: if it's a mock and we know the strategy_id is coming in status or something
                if event.eventType == "final_result" {
                    // Extract strategy_id and fetch
                    // For now, let's just wait for the stream to end and fetch a default or similar
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
