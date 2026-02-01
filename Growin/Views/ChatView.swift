import SwiftUI

struct ChatView: View {
    @Bindable var viewModel: ChatViewModel
    @State private var isUserScrolling = false
    @State private var showScrollToBottom = false
    @State private var showConversationList = false
    @Namespace private var animation
    private let bottomAnchorID = "bottom"
    
    var body: some View {
        VStack(spacing: 0) {
            // Integrated Chat Header
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(viewModel.selectedConversationId != nil ? "LIVE SESSION" : "READY")
                        .font(.caption)
                        .fontWeight(.semibold)
                        .foregroundStyle(.secondary)
                    
                    Text(viewModel.selectedConversationId != nil ? "Market Analysis" : "New Analysis")
                        .font(.title2)
                        .fontWeight(.bold)
                        .foregroundStyle(.primary)
                }
                
                Spacer()
                
                HStack(spacing: 12) {
                    Button(action: { showConversationList = true }) {
                        Image(systemName: "archivebox.fill")
                            .font(.system(size: 14, weight: .bold))
                            .frame(width: 40, height: 40)
                            .background(Color.white.opacity(0.05))
                            .clipShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .help("Conversation History")

                    Button(action: {
                        withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                            viewModel.startNewConversation()
                        }
                    }) {
                        HStack(spacing: 8) {
                            Image(systemName: "plus")
                            Text("New Analysis")
                        }
                        .font(.system(size: 13, weight: .medium))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Color.accentColor.opacity(0.1))
                        .foregroundStyle(Color.accentColor)
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    .buttonStyle(.plain)
                    .help("New Strategy Session")
                }
            }
            .padding(.horizontal)
            .padding(.top, 24)
            .padding(.bottom, 12)

            // Main Chat Area
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
                LazyVStack(alignment: .leading, spacing: 24) {
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
                        EnhancedTypingIndicator(statusText: "Synthesizing market intelligence...")
                            .id("typing")
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .padding(.horizontal, 8)
                    }
                    
                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchorID)
                }
                .padding(.horizontal)
                .padding(.top, 20)
            }
            .background(Color.clear)
            .glassEffect(.thin.interactive())
            .onChange(of: viewModel.messages.count) { _, _ in
                scrollToBottom(proxy: proxy)
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
                if viewModel.selectedConversationId != nil && viewModel.messages.isEmpty {
                    Task {
                        await viewModel.loadConversationHistory()
                    }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromTickerSearch"))) { notification in
                if let ticker = notification.userInfo?["ticker"] as? String {
                    Task { await handleTickerAnalysisCreation(ticker) }
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if showScrollToBottom {
                    Button(action: { scrollToBottom(proxy: proxy) }) {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 32, height: 32)
                            .background(Color.growinPrimary)
                            .clipShape(Circle())
                            .shadow(color: Color.growinPrimary.opacity(0.3), radius: 8)
                    }
                    .padding(.trailing, 20)
                    .padding(.bottom, 20)
                    .transition(.scale.combined(with: .opacity))
                }
            }
        }
    }
    
    private var inputSection: some View {
        VStack(spacing: 0) {
            Divider().opacity(0.1)
            
            VStack(spacing: 12) {
                // Top Utilities
                HStack {
                    AccountPicker(selectedAccount: $viewModel.selectedAccountType)
                        .scaleEffect(0.9)
                    
                    Spacer()
                    
                    if let error = viewModel.errorMessage {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.octagon.fill")
                            Text(error)
                                .lineLimit(1)
                        }
                        .font(.system(size: 11, weight: .medium, design: .rounded))
                        .foregroundStyle(Color.growinRed)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(Color.growinRed.opacity(0.1))
                        .clipShape(Capsule())
                        .onTapGesture { viewModel.errorMessage = nil }
                    }
                }
                .padding(.horizontal, 8)
                
                // Unified Input Field
                HStack(spacing: 12) {
                    TextField("Deploy query or command...", text: $viewModel.inputText, axis: .vertical)
                        .textFieldStyle(.plain)
                        .font(.system(.body, design: .rounded))
                        .padding(14)
                        .background(Color.white.opacity(0.04))
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .overlay(
                            RoundedRectangle(cornerRadius: 16)
                                .stroke(.white.opacity(0.08), lineWidth: 1)
                        )
                        .lineLimit(1...8)
                    
                    Button(action: {
                        if viewModel.isProcessing {
                            // Stop logic if implemented in VM
                        } else {
                            viewModel.sendMessage()
                        }
                    }) {
                        Image(systemName: viewModel.isProcessing ? "stop.fill" : "arrow.up.circle.fill")
                            .font(.system(size: 32))
                            .symbolRenderingMode(.hierarchical)
                            .foregroundStyle(viewModel.inputText.isEmpty && !viewModel.isProcessing ? .secondary : Color.growinPrimary)
                            .contentTransition(.symbolEffect(.replace))
                    }
                    .buttonStyle(.plain)
                    .disabled(viewModel.inputText.isEmpty && !viewModel.isProcessing)
                }
            }
            .padding()
            .background(.ultraThinMaterial.opacity(0.8))
            .glassEffect(.regular)
        }
    }
    
    private func handleTickerAnalysisCreation(_ ticker: String) async {
        viewModel.startNewConversation()
        viewModel.inputText = "Provide a deep technical and fundamental analysis of \(ticker). Synthesize potential entry vectors."
        viewModel.sendMessage()
    }
    
    private func scrollToBottom(proxy: ScrollViewProxy, animated: Bool = true) {
        if animated {
            withAnimation(.spring(response: 0.4, dampingFraction: 0.85)) {
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
        HStack(alignment: .top, spacing: 14) {
            if message.isUser {
                Spacer(minLength: 80)
            } else {
                PersonaIcon(name: message.agentName ?? "")
                    .scaleEffect(0.9)
                    .padding(.top, 4)
            }
            
            VStack(alignment: message.isUser ? .trailing : .leading, spacing: 6) {
                if !message.isUser {
                    HStack(spacing: 6) {
                        Text(message.displayName.uppercased())
                            .font(.system(size: 10, weight: .black))
                            .tracking(1)
                            .foregroundStyle(personaColor)
                        
                        if let model = message.modelName {
                            Text(model)
                                .font(.system(size: 9, weight: .bold, design: .monospaced))
                                .foregroundStyle(.secondary.opacity(0.6))
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Color.white.opacity(0.05))
                                .clipShape(RoundedRectangle(cornerRadius: 4))
                        }
                    }
                    .padding(.leading, 2)
                }
                
                if message.isUser {
                    Text(message.content)
                        .font(.system(.body, design: .rounded))
                        .padding(.horizontal, 16)
                        .padding(.vertical, 12)
                        .background(Color.growinPrimary)
                        .foregroundStyle(.white)
                        .clipShape(BubbleShape(isUser: true))
                        .shadow(color: Color.growinPrimary.opacity(0.2), radius: 5, y: 3)
                } else {
                    GlassCard(cornerRadius: 20) {
                        VStack(alignment: .leading, spacing: 16) {
                            MarkdownText(content: message.content)
                            
                            if let data = message.data {
                                RichDataView(data: data)
                            }
                            
                            if let toolCalls = message.toolCalls, !toolCalls.isEmpty {
                                ToolExecutionBlock(toolCalls: toolCalls)
                            }
                            
                            if !message.content.contains("Quick Actions") {
                                ScrollView(.horizontal, showsIndicators: false) {
                                    HStack(spacing: 10) {
                                        ForEach(defaultQuickActions, id: \.label) { action in
                                            Button(action: { onQuickAction?(action.prompt) }) {
                                                HStack(spacing: 6) {
                                                    Text(action.icon)
                                                    Text(action.label)
                                                }
                                                .font(.system(size: 11, weight: .bold, design: .rounded))
                                                .padding(.horizontal, 12)
                                                .padding(.vertical, 8)
                                                .background(Color.white.opacity(0.05))
                                                .clipShape(Capsule())
                                                .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
                                            }
                                            .buttonStyle(.plain)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            
            if !message.isUser {
                Spacer(minLength: 80)
            }
        }
    }
    
    private var defaultQuickActions: [QuickAction] {
        [
            QuickAction(icon: "📈", label: "Deep Analysis", prompt: "Conduct a deeper multi-timeframe analysis."),
            QuickAction(icon: "🛡️", label: "Risk Vectors", prompt: "Identify critical risk vectors for this scenario."),
            QuickAction(icon: "⚡", label: "Exec Strategy", prompt: "How should I structure this execution?")
        ]
    }
    
    private var personaColor: Color {
        switch message.agentName {
        case "Portfolio Analyst": return Color.Persona.analyst
        case "Risk Manager": return Color.Persona.risk
        case "Technical Trader": return Color.Persona.trader
        case "Execution Specialist": return Color.Persona.execution
        default: return Color.growinPrimary
        }
    }
}

struct BubbleShape: Shape {
    let isUser: Bool
    
    func path(in rect: CGRect) -> Path {
        UnevenRoundedRectangle(
            topLeadingRadius: 18,
            bottomLeadingRadius: isUser ? 18 : 2,
            bottomTrailingRadius: isUser ? 2 : 18,
            topTrailingRadius: 18
        ).path(in: rect)
    }
}

struct ToolExecutionBlock: View {
    let toolCalls: [ToolCall]
    
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("SYSTEM ACTION")
                .font(.caption2)
                .fontWeight(.medium)
                .foregroundStyle(.secondary)
            
            VStack(spacing: 8) {
                ForEach(toolCalls, id: \.id) { toolCall in
                    HStack(spacing: 12) {
                        Image(systemName: "terminal.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(Color.growinAccent)
                        
                        Text(formatToolName(toolCall.function.name))
                            .font(.system(size: 11, weight: .bold, design: .monospaced))
                            .foregroundStyle(.white.opacity(0.8))
                        
                        Spacer()
                        
                        Circle()
                            .fill(Color.growinGreen)
                            .frame(width: 6, height: 6)
                            .shadow(color: Color.growinGreen.opacity(0.5), radius: 3)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
                    .background(Color.black.opacity(0.2))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                }
            }
        }
    }
    
    private func formatToolName(_ name: String) -> String {
        name.replacingOccurrences(of: "_", with: " ").capitalized
    }
}


#Preview {
    NavigationStack {
        ChatView(viewModel: ChatViewModel())
    }
}
