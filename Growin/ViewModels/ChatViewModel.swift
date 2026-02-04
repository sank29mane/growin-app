import Combine
import Foundation
import SwiftUI

// Models moved to Models.swift

@Observable @MainActor
class ChatViewModel {
    var messages: [ChatMessageModel] = []
    var isProcessing = false
    var errorMessage: String?
    var inputText: String = ""
    var selectedConversationId: String? = nil
    var selectedAccountType: String = "all"
    var showConfigPrompt = false
    var missingConfigProvider: String?

    private let config = AppConfig.shared
    
    // Shared defaults for settings
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
        
        let userModel = ChatMessageModel(
            messageId: UUID().uuidString,
            role: "user",
            content: message,
            timestamp: ISO8601DateFormatter().string(from: Date()),
            toolCalls: nil,
            toolCallId: nil,
            agentName: nil,
            modelName: nil,
            data: nil
        )
        messages.append(userModel)

        let url = URL(string: "\(config.baseURL)/api/chat/message")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            let chatRequest = GrowinChatMessage(
                message: message,
                conversationId: selectedConversationId,
                modelName: effectiveModelName,
                coordinatorModel: defaults.string(forKey: "selectedCoordinatorModel") ?? "granite-tiny",
                apiKeys: [
                    "openai": defaults.string(forKey: "openaiApiKey") ?? "",
                    "gemini": defaults.string(forKey: "geminiApiKey") ?? ""
                ],
                accountType: selectedAccountType == "all" ? nil : selectedAccountType
            )

            request.httpBody = try JSONEncoder().encode(chatRequest)

            let configuration = URLSessionConfiguration.default
            configuration.timeoutIntervalForRequest = 120
            let session = URLSession(configuration: configuration)

            let (data, response) = try await session.data(for: request)

            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode != 200 {
                throw URLError(.badServerResponse)
            }

            let chatResponse = try JSONDecoder().decode(ChatResponse.self, from: data)

            if selectedConversationId == nil {
                selectedConversationId = chatResponse.conversationId
                defaults.set(chatResponse.conversationId, forKey: "currentConversationId")
            }

            messages.append(
                ChatMessageModel(
                    messageId: UUID().uuidString,
                    role: "assistant",
                    content: chatResponse.response,
                    timestamp: chatResponse.timestamp,
                    toolCalls: chatResponse.toolCalls,
                    toolCallId: nil,
                    agentName: chatResponse.agent,
                    modelName: nil,
                    data: chatResponse.data
                ))

            isProcessing = false

            if messages.count >= 2 && messages.count <= 4 {
                await generateTitle(for: chatResponse.conversationId)
            }

        } catch {
            errorMessage = "Failed to send message: \(error.localizedDescription)"
            isProcessing = false
            inputText = message
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
        var components = URLComponents(string: "\(config.baseURL)/conversations/\(conversationId)/generate-title")
        components?.queryItems = [URLQueryItem(name: "model_name", value: effectiveModelName)]

        guard let url = components?.url else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        _ = try? await URLSession.shared.data(for: request)
        NotificationCenter.default.post(name: NSNotification.Name("RefreshConversations"), object: nil)
    }
}
