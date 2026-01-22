import Combine
import Foundation
import SwiftUI

// Models moved to Models.swift

class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessageModel] = []
    @Published var isProcessing = false
    @Published var errorMessage: String?
    @Published var inputText: String = ""

    @AppStorage("currentConversationId") var currentConversationId: String?
    @AppStorage("selectedModel") private var selectedModel = "native-mlx"
    @AppStorage("selectedCoordinatorModel") private var selectedCoordinatorModel = "granite-tiny"
    @AppStorage("selectedProvider") private var selectedProvider = "mlx"

    @AppStorage("openaiApiKey") private var openaiApiKey = ""
    @AppStorage("geminiApiKey") private var geminiApiKey = ""

    @Published var showConfigPrompt = false
    @Published var missingConfigProvider: String?
    @Published var selectedAccountType: String = "all"  // Account picker: "all", "isa", "invest"

    private let baseURL = "http://127.0.0.1:8002"

    private var effectiveModelName: String {
        return selectedModel
    }

    func sendMessage() {
        guard !inputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }

        // Configuration Check
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
        inputText = ""  // Clear input immediately

        Task {
            await sendMessageAsync(userMessage)
        }
    }

    private func sendMessageAsync(_ message: String) async {
        let userMessage = message
        
        await MainActor.run {
            isProcessing = true
            errorMessage = nil
            
            // Optimization for local perception: add user message to UI immediately
            let userModel = ChatMessageModel(
                messageId: UUID().uuidString,
                role: "user",
                content: userMessage,
                timestamp: ISO8601DateFormatter().string(from: Date()),
                toolCalls: nil,
                toolCallId: nil,
                agentName: nil,
                modelName: nil,
                data: nil
            )
            messages.append(userModel)
        }

        let url = URL(string: "\(baseURL)/api/chat/message")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            let apiKeys = [
                "openai": openaiApiKey,
                "gemini": geminiApiKey,
            ]

            let chatRequest = GrowinChatMessage(
                message: userMessage,
                conversationId: currentConversationId,
                modelName: effectiveModelName,
                coordinatorModel: selectedCoordinatorModel,
                apiKeys: apiKeys,
                accountType: selectedAccountType == "all" ? nil : selectedAccountType
            )

            request.httpBody = try JSONEncoder().encode(chatRequest)
            print("Sending chat request to \(url)...")

            let configuration = URLSessionConfiguration.default
            configuration.timeoutIntervalForRequest = 120  // 2 minutes for local LLM
            let session = URLSession(configuration: configuration)

            let (data, response) = try await session.data(for: request)

            if let httpResponse = response as? HTTPURLResponse {
                print("Server responded with status code: \(httpResponse.statusCode)")
                if httpResponse.statusCode != 200 {
                    let errorBody = String(data: data, encoding: .utf8) ?? "no body"
                    print("❌ API Error (\(httpResponse.statusCode)): \(errorBody)")

                    // Try to parse JSON error structure
                    if let errorJson = try? JSONDecoder().decode([String: String].self, from: data),
                       let detail = errorJson["detail"]
                    {
                        throw URLError(
                            .badServerResponse, userInfo: [NSLocalizedDescriptionKey: detail])
                    }

                    // Try to parse complex error structure (from our new backend handler)
                    struct BackendError: Decodable {
                        let detail: ErrorDetail
                        struct ErrorDetail: Decodable {
                            let message: String
                        }
                    }

                    if let complexError = try? JSONDecoder().decode(BackendError.self, from: data) {
                        throw URLError(
                            .badServerResponse,
                            userInfo: [NSLocalizedDescriptionKey: complexError.detail.message])
                    }

                    throw URLError(
                        .badServerResponse,
                        userInfo: [
                            NSLocalizedDescriptionKey: "Server error: \(httpResponse.statusCode)"
                        ])
                }
            }

            let chatResponse = try JSONDecoder().decode(ChatResponse.self, from: data)
            print("Successfully decoded chat response from agent: \(chatResponse.agent)")

            await MainActor.run {
                // Update conversation ID if new
                if currentConversationId == nil {
                    currentConversationId = chatResponse.conversationId
                }

                // Add assistant response
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

                // Trigger auto-titling for new conversations (after first response)
                if messages.count >= 2 && messages.count <= 4 {
                    Task {
                        await generateTitle(for: chatResponse.conversationId)
                    }
                }
            }

        } catch let error as URLError {
            await MainActor.run {
                switch error.code {
                case .notConnectedToInternet:
                    errorMessage = "No internet connection. Check your network and try again."
                case .timedOut:
                    errorMessage = "Request timed out. The AI model may be loading or busy."
                case .cannotConnectToHost:
                    errorMessage =
                        "Cannot connect to backend. Make sure the server is running on port 8002."
                default:
                    errorMessage = "Network error: \(error.localizedDescription)"
                }
                isProcessing = false
                inputText = message
            }
        } catch {
            await MainActor.run {
                errorMessage = "Failed to send message: \(error.localizedDescription)"
                isProcessing = false
                inputText = message
            }
        }
    }

    func loadConversationHistory() async {
        guard let conversationId = currentConversationId else { return }

        let url = URL(string: "\(baseURL)/conversations/\(conversationId)/history")!

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let history = try JSONDecoder().decode([ChatMessageModel].self, from: data)

            await MainActor.run {
                messages = history
            }
        } catch {
            print("Failed to load history: \(error)")
        }
    }

    func startNewConversation() {
        currentConversationId = nil
        messages = []
        isProcessing = false
        errorMessage = nil
    }

    func generateTitle(for conversationId: String) async {
        var components = URLComponents(
            string: "\(baseURL)/conversations/\(conversationId)/generate-title")
        components?.queryItems = [URLQueryItem(name: "model_name", value: effectiveModelName)]

        guard let url = components?.url else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"

        do {
            let (_, _) = try await URLSession.shared.data(for: request)
            print("Title generated for conversation: \(conversationId)")
            // To refresh titles in sidebar, we might need a notification or a shared state refresh
            NotificationCenter.default.post(
                name: NSNotification.Name("RefreshConversations"), object: nil)
        } catch {
            print("Failed to generate title: \(error)")
        }
    }
}
