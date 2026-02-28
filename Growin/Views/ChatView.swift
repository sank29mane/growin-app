import SwiftUI

struct ChatView: View {
    @Bindable var viewModel: ChatViewModel
    @State private var isUserScrolling = false
    @State private var showScrollToBottom = false
    @State private var showConversationList = false
    @Namespace private var animation
    private let bottomAnchorID = "bottom"
    
    var body: some View {
        ZStack {
            // Background gradient
            LinearGradient(
                colors: [
                    Color.black,
                    Color(red: 0.05, green: 0.05, blue: 0.15)
                ],
                startPoint: .top,
                endPoint: .bottom
            )
            .ignoresSafeArea()
            
            VStack(spacing: 0) {
                // Show welcome view when no messages
                if viewModel.messages.isEmpty && !viewModel.isProcessing {
                    WelcomeView { prompt in
                        viewModel.inputText = prompt
                        viewModel.sendMessage()
                    }
                    .transition(.opacity.combined(with: .scale(scale: 0.95)))
                } else {
                    messagesSection
                }
                
                inputSection
            }
        }
        .navigationTitle("AI Trading Assistant")
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button(action: {
                    showConversationList = true
                }) {
                    Image(systemName: "list.bullet")
                }
                .help("Conversation History")
                .accessibilityLabel("Conversation History")
            }
            
            ToolbarItem(placement: .automatic) {
                Button(action: {
                    withAnimation(.spring()) {
                        viewModel.startNewConversation()
                    }
                }) {
                    Image(systemName: "plus.circle")
                }
                .help("New Chat")
                .accessibilityLabel("New Chat")
            }
        }
        .sheet(isPresented: $viewModel.showConfigPrompt) {
            ConfigView(provider: viewModel.missingConfigProvider)
        }
        .sheet(isPresented: $showConversationList) {
            ConversationListView(selectedConversationId: $viewModel.selectedConversationId)
        }
    }
    
    private var messagesSection: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    ForEach(viewModel.messages) { message in
                        ChatBubble(message: message, onQuickAction: { prompt in
                            viewModel.inputText = prompt
                            viewModel.sendMessage()
                        })
                            .id(message.id)
                            .transition(.asymmetric(
                                insertion: .opacity.combined(with: .move(edge: .bottom)),
                                removal: .opacity
                            ))
                    }
                    
                    if viewModel.isProcessing {
                        EnhancedTypingIndicator(statusText: viewModel.streamingStatus ?? "Synthesizing market data...")
                            .id("typing")
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .padding(.horizontal, 8)
                    }
                    
                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchorID)
                }
                .padding()
            }
            .onChange(of: viewModel.messages.last?.content) { _, _ in
                if !isUserScrolling {
                    scrollToBottom(proxy: proxy)
                }
            }
            .onChange(of: viewModel.isProcessing) { _, isProcessing in
                if isProcessing {
                    scrollToBottom(proxy: proxy)
                }
            }
            .onChange(of: viewModel.selectedConversationId) { _, newId in
                Task {
                    if newId != nil {
                        await viewModel.loadConversationHistory()
                    } else {
                        viewModel.startNewConversation()
                    }
                }
            }
            .onAppear {
                scrollToBottom(proxy: proxy, animated: false)
            }
            .task {
                if viewModel.selectedConversationId != nil && viewModel.messages.isEmpty {
                    await viewModel.loadConversationHistory()
                }
            }
        }
    }
    
    private var inputSection: some View {
        VStack(spacing: 0) {
            if let error = viewModel.errorMessage {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill")
                    Text(error)
                        .lineLimit(2)
                    Spacer()
                    Button("Retry") {
                        viewModel.errorMessage = nil
                        viewModel.sendMessage()
                    }
                    .font(.caption.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color.red)
                    .cornerRadius(8)
                }
                .font(.caption)
                .foregroundStyle(.white)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
                .background(Color.red.opacity(0.2))
            }
            
            VStack(spacing: 10) {
                HStack {
                    AccountPicker(selectedAccount: $viewModel.selectedAccountType)
                    Spacer()
                }
                .padding(.horizontal, 4)
                
                HStack(spacing: 12) {
                    TextField("Ask about your portfolio...", text: $viewModel.inputText, axis: .vertical)
                        .textFieldStyle(.plain)
                        .padding(12)
                        .background(Color.white.opacity(0.05))
                        .cornerRadius(12)
                        .overlay(
                            RoundedRectangle(cornerRadius: 12)
                                .stroke(Color.white.opacity(0.1), lineWidth: 1)
                        )
                        .lineLimit(1...5)
                        .onSubmit {
                            viewModel.sendMessage()
                        }
                    
                    Button(action: {
                        viewModel.sendMessage()
                    }) {
                        ZStack {
                            if viewModel.inputText.isEmpty && !viewModel.isProcessing {
                                Circle()
                                    .fill(Color.gray.opacity(0.2))
                                    .frame(width: 40, height: 40)
                            } else {
                                Circle()
                                    .fill(LinearGradient(
                                        colors: [.blue, .blue.opacity(0.8)],
                                        startPoint: .topLeading,
                                        endPoint: .bottomTrailing
                                    ))
                                    .frame(width: 40, height: 40)
                            }
                            
                            Image(systemName: viewModel.isProcessing ? "stop.fill" : "arrow.up")
                                .font(.system(size: 16, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.inputText.isEmpty && !viewModel.isProcessing)
                }
            }
            .padding()
            .background(.ultraThinMaterial)
        }
    }
    
    private func scrollToBottom(proxy: ScrollViewProxy, animated: Bool = true) {
        if animated {
            withAnimation(.spring()) {
                proxy.scrollTo(bottomAnchorID, anchor: .bottom)
            }
        } else {
            proxy.scrollTo(bottomAnchorID, anchor: .bottom)
        }
    }
}

struct ChatBubble: View {
    let message: ChatMessageModel
    var onQuickAction: ((String) -> Void)? = nil
    
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            if message.isUser {
                Spacer(minLength: 60)
            } else {
                PersonaIcon(name: message.agentName ?? "")
                    .padding(.top, 4)
            }
            
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 6) {
                if !message.isUser {
                    HStack(spacing: 4) {
                        Text(message.displayName)
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(personaColor)
                        
                        if let model = message.modelName {
                            Text("• \(model)")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.leading, 4)
                }
                
                if message.isUser {
                    Text(message.content)
                        .padding(14)
                        .background(
                            LinearGradient(
                                colors: [Color.blue, Color.blue.opacity(0.8)],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .foregroundStyle(.white)
                        .cornerRadius(18, corners: [.topLeft, .topRight, .bottomLeft])
                } else {
                    GlassCard(cornerRadius: 16) {
                        VStack(alignment: .leading, spacing: 12) {
                            if !message.content.isEmpty {
                                MarkdownText(content: message.content)
                                    .foregroundStyle(.white.opacity(0.9))
                            }
                            
                            // Reasoning Trace UI (Wave 2)
                            if let data = message.data {
                                LegacyReasoningTraceView(data: data)
                            }
                            
                            // Rich Data Visualization
                            if let data = message.data {
                                RichDataView(data: data)
                            }
                            
                            if let toolCalls = message.toolCalls, !toolCalls.isEmpty {
                                ToolExecutionBlock(toolCalls: toolCalls)
                            }
                            
                            if !message.content.contains("Quick Actions") && !message.content.isEmpty {
                                QuickActionButtons(actions: defaultQuickActions) { prompt in
                                    onQuickAction?(prompt)
                                }
                            }
                        }
                    }
                }
                
                Text(formatTimestamp(message.timestamp))
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
            }
            
            if !message.isUser {
                Spacer(minLength: 60)
            }
        }
    }
    
    private var defaultQuickActions: [QuickAction] {
        [
            QuickAction(icon: "📊", label: "Deep Dive", prompt: "Give me more details about this"),
            QuickAction(icon: "🎯", label: "Trading Ideas", prompt: "What trades should I consider?"),
            QuickAction(icon: "⚠️", label: "Risk Check", prompt: "What are the risks I should know about?")
        ]
    }
    
    private var personaColor: Color {
        switch message.agentName {
        case "Portfolio Analyst": return Color.Persona.analyst
        case "Risk Manager": return Color.Persona.risk
        case "Technical Trader": return Color.Persona.trader
        case "Execution Specialist": return Color.Persona.execution
        default: return .blue
        }
    }
    
    private func formatTimestamp(_ timestamp: String) -> String {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: timestamp) {
            let timeFormatter = DateFormatter()
            timeFormatter.setLocalizedDateFormatFromTemplate("HH:mm")
            return timeFormatter.string(from: date)
        }
        return timestamp
    }
}

struct LegacyReasoningTraceView: View {
    let data: MarketContextData
    
    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 10) {
                if let quant = data.quant {
                    ReasoningStepRow(
                        agent: "QuantAgent",
                        status: "ANALYZED",
                        icon: "chart.line.uptrend.xyaxis",
                        color: Color.Persona.trader,
                        detail: "Signal: \(quant.signal)"
                    )
                }
                
                if let forecast = data.forecast {
                    ReasoningStepRow(
                        agent: "ForecastingAgent",
                        status: "PREDICTED",
                        icon: "brain.head.profile",
                        color: .green,
                        detail: "Trend: \(forecast.trend)"
                    )
                }
                
                if let research = data.research {
                    ReasoningStepRow(
                        agent: "ResearchAgent",
                        status: "SCANNED",
                        icon: "newspaper.fill",
                        color: Color.Persona.risk,
                        detail: "Sentiment: \(research.sentimentLabel)"
                    )
                }
                
                if let whale = data.whale {
                    ReasoningStepRow(
                        agent: "WhaleAgent",
                        status: "DETECTED",
                        icon: "water.waves",
                        color: .indigo,
                        detail: whale.sentimentImpact
                    )
                }
            }
            .padding(.vertical, 8)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 14))
                Text("Reasoning Chain")
                    .font(.system(size: 11, weight: .bold))
                Spacer()
            }
            .foregroundStyle(.white.opacity(0.6))
        }
        .padding(10)
        .background(Color.white.opacity(0.03))
        .cornerRadius(12)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
    }
}

struct ToolExecutionBlock: View {
    let toolCalls: [ToolCall]
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("RESEARCH STEPS")
                .font(.system(size: 9, weight: .black))
                .foregroundStyle(.secondary)
            
            ForEach(toolCalls, id: \.id) { toolCall in
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass.circle.fill")
                        .font(.system(size: 14))
                        .foregroundStyle(.blue)
                    
                    Text(formatToolName(toolCall.function.name))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.7))
                    
                    Spacer()
                    
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(.green)
                }
                .padding(8)
                .background(Color.white.opacity(0.05))
                .cornerRadius(8)
            }
        }
        .padding(8)
        .background(Color.black.opacity(0.2))
        .cornerRadius(10)
    }
    
    private func formatToolName(_ name: String) -> String {
        name.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct ReasoningStepRow: View {
    let agent: String
    let status: String
    let icon: String
    let color: Color
    let detail: String
    
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 12))
                .foregroundStyle(color)
                .frame(width: 24, height: 24)
                .background(color.opacity(0.1))
                .clipShape(Circle())
            
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(agent)
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.white)
                    Text(status)
                        .font(.system(size: 8, weight: .black))
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(color.opacity(0.2))
                        .foregroundStyle(color)
                        .cornerRadius(3)
                }
                
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            
            Spacer()
        }
    }
}
