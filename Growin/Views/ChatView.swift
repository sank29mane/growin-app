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
                    // Start new conversation
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
                    
                    // Enhanced typing indicator
                    if viewModel.isProcessing {
                        EnhancedTypingIndicator(statusText: "Synthesizing market data...")
                            .id("typing")
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .padding(.horizontal, 8)
                    }
                    
                    // Bottom anchor
                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchorID)
                }
                .padding()
            }
            .onChange(of: viewModel.messages.count) { _, _ in
                scrollToBottom(proxy: proxy)
            }
            .onChange(of: viewModel.isProcessing) { _, isProcessing in
                if isProcessing {
                    scrollToBottom(proxy: proxy)
                }
            }
            // Fix: React to selectedConversationId changes
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
                // Load conversation history if we have a conversation ID
                if viewModel.selectedConversationId != nil && viewModel.messages.isEmpty {
                    await viewModel.loadConversationHistory()
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromTickerSearch"))) { notification in
                if let ticker = notification.userInfo?["ticker"] as? String {
                    Task {
                        await handleTickerAnalysisCreation(ticker)
                    }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromChart"))) { notification in
                if let chartContext = notification.userInfo as? [String: Any] {
                    Task {
                        await handleChartChatCreation(chartContext)
                    }
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if showScrollToBottom {
                    Button(action: {
                        scrollToBottom(proxy: proxy)
                        showScrollToBottom = false
                    }) {
                        Image(systemName: "arrow.down.circle.fill")
                            .font(.system(size: 36))
                            .foregroundStyle(.white)
                            .background(Circle().fill(Color.blue))
                            .shadow(radius: 4)
                    }
                    .help("Scroll to bottom")
                    .accessibilityLabel("Scroll to bottom")
                    .padding(.trailing, 20)
                    .padding(.bottom, 80)
                    .transition(.scale.combined(with: .opacity))
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
                // Account Picker
                HStack {
                    AccountPicker(selectedAccount: $viewModel.selectedAccountType)
                    Spacer()
                }
                .padding(.horizontal, 4)
                
                // Input Field
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
                            if viewModel.inputText.isEmpty {
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
                    .help(viewModel.isProcessing ? "Stop generating" : "Send message")
                    .accessibilityLabel(viewModel.isProcessing ? "Stop generating" : "Send message")
                    .disabled(viewModel.inputText.isEmpty && !viewModel.isProcessing)
                }
            }
            .padding()
            .background(.ultraThinMaterial)
        }
    }
    
    
    private func handleTickerAnalysisCreation(_ ticker: String) async {
        viewModel.startNewConversation()
        let prompt = "Please analyze the financial health and technical signals for \(ticker). Should I consider a trade here?"
        viewModel.inputText = prompt
        viewModel.sendMessage()
    }

    private func handleChartChatCreation(_ chartContext: [String: Any]) async {
        // Start a new conversation with chart context
        viewModel.startNewConversation()
        
        // Create an initial message with chart analysis context
        if let symbol = chartContext["symbol"] as? String,
           let timeframe = chartContext["timeframe"] as? String,
           let currentPrice = chartContext["currentPrice"] as? Double {
            
            let contextMessage = "I'm looking at the \(symbol) chart on \(timeframe) timeframe. The current price is £\(String(format: "%.2f", currentPrice)). What analysis or insights can you provide about this stock?"
            
            // Send the context message
            viewModel.inputText = contextMessage
            viewModel.sendMessage()
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
                // Agent/Persona Header
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
                
                // Content Card
                if message.isUser {
                    // User message - right aligned with blue gradient
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
                        .cornerRadius(18)
                        .cornerRadius(18, corners: [.topLeft, .topRight, .bottomLeft])
                } else {
                    // AI message - glass card
                    GlassCard(cornerRadius: 16) {
                        VStack(alignment: .leading, spacing: 12) {
                            MarkdownText(content: message.content)
                                .foregroundStyle(.white.opacity(0.9))
                            
                            // Rich Data Visualization
                            if let data = message.data {
                                RichDataView(data: data)
                            }
                            
                            if let toolCalls = message.toolCalls, !toolCalls.isEmpty {
                                ToolExecutionBlock(toolCalls: toolCalls)
                            }
                            
                            // Quick action buttons (if no markdown suggestions)
                            if !message.content.contains("Quick Actions") {
                                QuickActionButtons(actions: defaultQuickActions) { prompt in
                                    onQuickAction?(prompt)
                                }
                            }
                        }
                    }
                }
                
                // Timestamp
                Text(formatTimestamp(message.timestamp))
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 4)
                    .accessibilityLabel("Sent at \(formatTimestamp(message.timestamp))")
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

// Extension for rounded corners on specific sides
extension View {
    func cornerRadius(_ radius: CGFloat, corners: UIRectCorner) -> some View {
        clipShape(RoundedCorner(radius: radius, corners: corners))
    }
}

struct RoundedCorner: Shape {
    var radius: CGFloat = .infinity
    var corners: UIRectCorner = .allCorners

    func path(in rect: CGRect) -> Path {
        let path = NSBezierPath(
            roundedRect: rect,
            byRoundingCorners: corners,
            cornerRadii: CGSize(width: radius, height: radius)
        )
        return Path(path.cgPath)
    }
}

// NSBezierPath extension for macOS
extension NSBezierPath {
    convenience init(roundedRect rect: CGRect, byRoundingCorners corners: UIRectCorner, cornerRadii: CGSize) {
        self.init()
        
        let radius = cornerRadii.width
        
        let topLeft = corners.contains(.topLeft) ? radius : 0
        let topRight = corners.contains(.topRight) ? radius : 0
        let bottomLeft = corners.contains(.bottomLeft) ? radius : 0
        let bottomRight = corners.contains(.bottomRight) ? radius : 0
        
        move(to: CGPoint(x: rect.minX + topLeft, y: rect.minY))
        
        // Top edge
        line(to: CGPoint(x: rect.maxX - topRight, y: rect.minY))
        if topRight > 0 {
            appendArc(withCenter: CGPoint(x: rect.maxX - topRight, y: rect.minY + topRight),
                     radius: topRight,
                     startAngle: -90,
                     endAngle: 0,
                     clockwise: false)
        }
        
        // Right edge
        line(to: CGPoint(x: rect.maxX, y: rect.maxY - bottomRight))
        if bottomRight > 0 {
            appendArc(withCenter: CGPoint(x: rect.maxX - bottomRight, y: rect.maxY - bottomRight),
                     radius: bottomRight,
                     startAngle: 0,
                     endAngle: 90,
                     clockwise: false)
        }
        
        // Bottom edge
        line(to: CGPoint(x: rect.minX + bottomLeft, y: rect.maxY))
        if bottomLeft > 0 {
            appendArc(withCenter: CGPoint(x: rect.minX + bottomLeft, y: rect.maxY - bottomLeft),
                     radius: bottomLeft,
                     startAngle: 90,
                     endAngle: 180,
                     clockwise: false)
        }
        
        // Left edge
        line(to: CGPoint(x: rect.minX, y: rect.minY + topLeft))
        if topLeft > 0 {
            appendArc(withCenter: CGPoint(x: rect.minX + topLeft, y: rect.minY + topLeft),
                     radius: topLeft,
                     startAngle: 180,
                     endAngle: 270,
                     clockwise: false)
        }
        
        close()
    }
    
    var cgPath: CGPath {
        let path = CGMutablePath()
        var points = [CGPoint](repeating: .zero, count: 3)
        
        for i in 0..<elementCount {
            let type = element(at: i, associatedPoints: &points)
            
            switch type {
            case .moveTo:
                path.move(to: points[0])
            case .lineTo:
                path.addLine(to: points[0])
            case .curveTo:
                path.addCurve(to: points[2], control1: points[0], control2: points[1])
            case .closePath:
                path.closeSubpath()
            default:
                break
            }
        }
        
        return path
    }
}

// UIRectCorner equivalent for macOS
struct UIRectCorner: OptionSet {
    let rawValue: Int
    
    static let topLeft = UIRectCorner(rawValue: 1 << 0)
    static let topRight = UIRectCorner(rawValue: 1 << 1)
    static let bottomLeft = UIRectCorner(rawValue: 1 << 2)
    static let bottomRight = UIRectCorner(rawValue: 1 << 3)
    static let allCorners: UIRectCorner = [.topLeft, .topRight, .bottomLeft, .bottomRight]
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
                        .accessibilityHidden(true)
                    
                    Text(formatToolName(toolCall.function.name))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.white.opacity(0.7))
                    
                    Spacer()
                    
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(.green)
                        .accessibilityHidden(true)
                }
                .padding(8)
                .background(Color.white.opacity(0.05))
                .cornerRadius(8)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Completed research step: \(formatToolName(toolCall.function.name))")
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


#Preview {
    NavigationStack {
        ChatView(viewModel: ChatViewModel())
    }
}
