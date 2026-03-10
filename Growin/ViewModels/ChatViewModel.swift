import Combine
import Foundation
import SwiftUI

@Observable @MainActor
class ChatViewModel {
    var messages: [ChatMessageModel] = []
    var isProcessing = false
    var streamingStatus: String?
    var errorMessage: String?
    var inputText: String = ""
    var selectedConversationId: String? = nil
    var selectedAccountType: String = "isa"
    var showConfigPrompt = false
    var missingConfigProvider: String?

    // Active reasoning tracing for current message
    var activeReasoningSteps: [ReasoningStep] = []

    private let config = AppConfig.shared
    private let agentClient = AgentClient()
    private let defaults = UserDefaults.standard

    init() {
        self.selectedConversationId = defaults.string(forKey: "currentConversationId")
    }

    private var effectiveModelName: String {
        defaults.string(forKey: "selectedModel") ?? "native-mlx"
    }

    func sendMessage() {
        guard !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }

        let openaiApiKey = defaults.string(forKey: "openaiApiKey") ?? ""
        let geminiApiKey = defaults.string(forKey: "geminiApiKey") ?? ""
        let selectedProvider = defaults.string(forKey: "selectedProvider") ?? "mlx"

        if selectedProvider == "openai" && openaiApiKey.isEmpty {
            missingConfigProvider = "OpenAI"
            showConfigPrompt = true
            return
        }

        if selectedProvider == "gemini" && geminiApiKey.isEmpty {
            missingConfigProvider = "Gemini"
            showConfigPrompt = true
            return
        }

        let userMessage = inputText
        inputText = ""

        Task {
            await sendMessageAsync(userMessage)
        }
    }

    private func sendMessageAsync(_ message: String) async {
        isProcessing = true
        errorMessage = nil
        streamingStatus = "Planning..."
        activeReasoningSteps = []

        let userModel = ChatMessageModel(
            messageId: UUID().uuidString,
            role: "user",
            content: message,
            timestamp: ISO8601DateFormatter().string(from: Date()),
            toolCalls: nil,
            toolCallId: nil,
            agentName: nil,
            modelName: nil,
            data: nil,
            reasoningSteps: nil
        )
        messages.append(userModel)

        // Create placeholder for assistant response
        let assistantMessageId = UUID().uuidString
        let assistantModel = ChatMessageModel(
            messageId: assistantMessageId,
            role: "assistant",
            content: "",
            timestamp: ISO8601DateFormatter().string(from: Date()),
            toolCalls: nil,
            toolCallId: nil,
            agentName: "DecisionAgent",
            modelName: effectiveModelName,
            data: nil,
            reasoningSteps: nil
        )
        messages.append(assistantModel)

        var accumulatedContent = ""

        let stream = agentClient.streamMessage(
            query: message,
            conversationId: selectedConversationId,
            model: effectiveModelName,
            accountType: selectedAccountType
        )

        for await event in stream {
            switch event {
            case .token(let token):
                accumulatedContent += token
                updateAssistantMessage(id: assistantMessageId, content: accumulatedContent)
            case .status(let status):
                streamingStatus = status
            case .telemetry(let telemetry):
                handleTelemetry(telemetry)
            case .meta(let meta):
                if let convId = meta["conversation_id"]?.value as? String {
                    if selectedConversationId == nil {
                        selectedConversationId = convId
                        defaults.set(convId, forKey: "currentConversationId")
                    }
                }
            case .error(let error):
                errorMessage = error
            case .done:
                isProcessing = false
                streamingStatus = nil
                updateAssistantMessage(
                    id: assistantMessageId, content: accumulatedContent,
                    reasoningSteps: activeReasoningSteps)
                activeReasoningSteps = []
            }
        }

        if messages.count >= 2 && messages.count <= 4, let convId = selectedConversationId {
            await generateTitle(for: convId)
        }
    }

    private func handleTelemetry(_ telemetry: [String: AnySendable]) {
        // Architecture Resilience: Ensure all telemetry parsing and UI updates 
        // are serialized on the MainActor to prevent 'AccessSet::insert' data races.
        Task { @MainActor in
            let sender = telemetry["sender"]?.value as? String ?? "Agent"
            let subject = telemetry["subject"]?.value as? String ?? ""
            let payload = telemetry["payload"]?.value as? [String: Any] ?? [:]

            let timestamp = Date().timeIntervalSince1970
            var newStep: ReasoningStep? = nil

            if subject == "agent_started" {
                let agent = payload["agent"] as? String ?? sender
                streamingStatus = "Agent \(agent) starting..."
                newStep = ReasoningStep(agent: agent, action: "Started", content: nil, timestamp: timestamp)
            } else if subject == "agent_complete" {
                let agent = payload["agent"] as? String ?? sender
                let success = payload["success"] as? Bool ?? true
                if success {
                    streamingStatus = "Agent \(agent) finished."
                    newStep = ReasoningStep(agent: agent, action: "Finished successfully", content: nil, timestamp: timestamp)
                } else {
                    streamingStatus = "Agent \(agent) failed."
                    newStep = ReasoningStep(agent: agent, action: "Failed", content: nil, timestamp: timestamp)
                }
            } else if subject == "intent_classified" {
                if let intent = payload["intent"] as? [String: Any],
                    let type = intent["type"] as? String {
                    streamingStatus = "Intent: \(type)"
                    newStep = ReasoningStep(agent: sender, action: "Classified Intent: \(type)", content: nil, timestamp: timestamp)
                }
            } else if subject == "tool_call" {
                let tool = payload["tool"] as? String ?? "Unknown Tool"
                streamingStatus = "Executing \(tool)..."
                newStep = ReasoningStep(agent: sender, action: "Calling \(tool)", content: nil, timestamp: timestamp)
            } else if subject == "reasoning" || subject == "thought" {
                let thought = payload["content"] as? String ?? "Processing..."
                newStep = ReasoningStep(agent: sender, action: "Thinking", content: thought, timestamp: timestamp)
            } else if subject == "debate" {
                let turn = payload["content"] as? String ?? ""
                newStep = ReasoningStep(agent: sender, action: "Debating", content: turn, timestamp: timestamp)
            }
            
            if let step = newStep {
                // SOTA: Use withAnimation to provide kinetic feedback while keeping access serialized
                withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                    activeReasoningSteps.append(step)
                }
            }
        }
    }

    private func updateAssistantMessage(
        id: String, content: String, reasoningSteps: [ReasoningStep]? = nil
    ) {
        if let index = messages.firstIndex(where: { $0.messageId == id }) {
            let old = messages[index]
            messages[index] = ChatMessageModel(
                messageId: old.messageId,
                role: old.role,
                content: content,
                timestamp: old.timestamp,
                toolCalls: old.toolCalls,
                toolCallId: old.toolCallId,
                agentName: old.agentName,
                modelName: old.modelName,
                data: old.data,
                reasoningSteps: reasoningSteps ?? old.reasoningSteps
            )
        }
    }

    func loadConversationHistory() async {
        guard let conversationId = selectedConversationId else { return }
        let url = URL(string: "\(config.baseURL)/conversations/\(conversationId)/history")!

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let history = try JSONDecoder().decode([ChatMessageModel].self, from: data)
            self.messages = history
            self.errorMessage = nil
        } catch {
            self.errorMessage = "Failed to load history."
        }
    }

    func startNewConversation() {
        selectedConversationId = nil
        defaults.removeObject(forKey: "currentConversationId")
        messages = []
        isProcessing = false
        errorMessage = nil
    }

    func generateTitle(for conversationId: String) async {
        var components = URLComponents(
            string: "\(config.baseURL)/conversations/\(conversationId)/generate-title")
        components?.queryItems = [URLQueryItem(name: "model_name", value: effectiveModelName)]

        guard let url = components?.url else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        _ = try? await URLSession.shared.data(for: request)
        NotificationCenter.default.post(
            name: NSNotification.Name("RefreshConversations"), object: nil)
    }
}
