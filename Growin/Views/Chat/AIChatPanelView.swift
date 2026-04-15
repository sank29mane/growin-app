import SwiftUI

/// AIChatPanelView: The conversational hub for the Sovereign Desktop UI.
/// Incorporates rounded "Mac-native" bubble aesthetics while retaining the core dark-theme color palette (Brutal Chartreuse, Cyan).
struct AIChatPanelView: View {
    @State private var chatInput: String = ""
    @State private var selectedAccountType: String = "all"
    
    // State to simulate switching into an active chat view vs the "Home" state
    @State private var hasStartedChat: Bool = false
    @State private var isThinking: Bool = false
    @State private var thinkingStatus: String = "Analyzing..."
    @State private var chatMessages: [ChatMessage] = []
    
    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            
            VStack(spacing: 0) {
                // Top Navigation/Status Area (Minimal)
                HStack {
                    Spacer()
                    Button(action: { /* Clear Chat / New Subject */ }) {
                        Image(systemName: "square.and.pencil")
                            .font(.system(size: 14))
                            .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("New Chat")
                    .accessibilityHint("Clears current conversation and starts a new subject")
                    .accessibilityAddTraits(.isButton)
                    .padding(16)
                }
                
                if hasStartedChat {
                    // Active Chat Interface
                    VStack(spacing: 0) {
                        ActiveChatView(messages: $chatMessages, onSuggestionTap: { prompt in
                            startChat(with: prompt)
                        })
                        
                        if isThinking {
                            EnhancedTypingIndicator(statusText: thinkingStatus)
                                .padding(.horizontal, 24)
                                .padding(.bottom, 12)
                        }
                    }
                } else {
                    // Discovery Home Dashboard (Welcome Screen)
                    Spacer()
                    WelcomeView(onSuggestionTap: { prompt in
                        startChat(with: prompt)
                    })
                    Spacer()
                }
                
                // Bottom Input Area
                VStack(spacing: 12) {
                    // Account Picker
                    HStack {
                        AccountPicker(selectedAccount: $selectedAccountType)
                        Spacer()
                    }
                    .padding(.horizontal, 24)
                    
                    // Chat Input Box
                    HStack {
                        TextField("Ask about your portfolio or market strategies...", text: $chatInput)
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                            .foregroundStyle(Color.brutalOffWhite)
                            .textFieldStyle(.plain)
                            .onSubmit {
                                if !chatInput.isEmpty {
                                    startChat(with: chatInput)
                                    chatInput = ""
                                }
                            }
                        
                        Button(action: {
                            if !chatInput.isEmpty {
                                startChat(with: chatInput)
                                chatInput = ""
                            }
                        }) {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(chatInput.isEmpty ? Color.gray : Color.black)
                                .padding(8)
                                .background(chatInput.isEmpty ? Color.white.opacity(0.1) : Color.cyan)
                                .clipShape(Circle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Send Message")
                        .accessibilityHint("Sends the drafted message to the AI")
                        .accessibilityAddTraits(.isButton)
                        .disabled(chatInput.isEmpty)
                    }
                    .padding(16)
                    .background(Color.white.opacity(0.03))
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16)
                            .stroke(Color.white.opacity(0.1), lineWidth: 1)
                    )
                    .padding(.horizontal, 24)
                    .padding(.bottom, 24)
                }
            }
        }
    }
    
    private func startChat(with prompt: String) {
        if prompt.isEmpty { return }
        
        withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
            hasStartedChat = true
            chatMessages.append(ChatMessage(id: UUID(), text: prompt, isUser: true))
            isThinking = true
            thinkingStatus = "Synthesizing strategy..."
        }
        
        // Mock multi-step reasoning
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            thinkingStatus = "Analyzing technical signals..."
        }
        
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.4) {
            thinkingStatus = "Optimizing risk parameters..."
        }
        
        // Mock System Response containing dynamic tiles/data
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.6) {
            withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                isThinking = false
                let mockActions = [
                    QuickAction(icon: "📊", label: "Position Details", prompt: "Show me position breakdown"),
                    QuickAction(icon: "📈", label: "Full Analysis", prompt: "Perform deep dive for this asset")
                ]
                chatMessages.append(ChatMessage(id: UUID(), text: "Analyzing your portfolio strategy. Here's what I found for the selected instrument.", isUser: false, hasDynamicTiles: true, quickActions: mockActions))
            }
        }
    }
}

// MARK: - Active Chat View
private struct ActiveChatView: View {
    @Binding var messages: [ChatMessage]
    var onSuggestionTap: (String) -> Void
    
    var body: some View {
        ScrollView {
            LazyVStack(spacing: 24) {
                ForEach(messages) { message in
                    ChatMessageRow(message: message, onSuggestionTap: onSuggestionTap)
                }
            }
            .padding(24)
        }
    }
}


private struct ChatMessageRow: View {
    let message: ChatMessage
    var onSuggestionTap: (String) -> Void
    
    var body: some View {
        HStack(alignment: .bottom, spacing: 12) {
            if message.isUser {
                Spacer()
                Text(message.text)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                    .foregroundStyle(Color.black)
                    .padding(16)
                    .background(Color.cyan)
                    .clipShape(RoundedCornerShape(radius: 16, corners: [.topLeft, .topRight, .bottomLeft]))
                
                Image(systemName: "person.circle.fill")
                    .font(.system(size: 24))
                    .foregroundStyle(Color.white.opacity(0.4))
            } else {
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 24))
                    .foregroundStyle(Color.cyan)
                    .shadow(color: Color.cyan.opacity(0.4), radius: 8)
                
                VStack(alignment: .leading, spacing: 12) {
                    Text(message.text)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                        .foregroundStyle(Color.brutalOffWhite)
                        .lineSpacing(4)
                        .padding(16)
                        .background(Color.white.opacity(0.06))
                        .clipShape(RoundedCornerShape(radius: 16, corners: [.topRight, .bottomLeft, .bottomRight]))
                    
                    if let quickActions = message.quickActions, !quickActions.isEmpty {
                        QuickActionButtons(actions: quickActions, onTap: onSuggestionTap)
                    }
                    
                    if message.hasDynamicTiles {
                        // Example dynamic inline tile UI from the agent
                        HStack(spacing: 12) {
                            InlineActionTile(title: "Execute Trace", icon: "bolt.fill", color: .brutalChartreuse)
                            InlineActionTile(title: "View Ledger", icon: "doc.text.fill", color: .cyan)
                        }
                        .padding(.leading, 8)
                    }
                }
                Spacer()
            }
        }
    }
}

private struct InlineActionTile: View {
    let title: String
    let icon: String
    let color: Color
    
    var body: some View {
        Button(action: {}) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 10))
                Text(title)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(color.opacity(0.1))
            .foregroundStyle(color)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(color.opacity(0.3), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title) Action")
        .accessibilityHint("Executes the \(title) action")
        .accessibilityAddTraits(.isButton)
    }
}

// MARK: - Helpers

struct ChatMessage: Identifiable {
    let id: UUID
    let text: String
    let isUser: Bool
    var hasDynamicTiles: Bool = false
    var quickActions: [QuickAction]? = nil
}

private struct RoundedCornerShape: Shape {
    var radius: CGFloat = .infinity
    
    let cornersList: [Corner]
    enum Corner { case topLeft, topRight, bottomLeft, bottomRight }
    
    init(radius: CGFloat, corners: [Corner]) {
        self.radius = radius
        self.cornersList = corners
    }
    
    func path(in rect: CGRect) -> Path {
        var path = Path()
        
        // Define radii
        let tl = cornersList.contains(.topLeft) ? radius : 0
        let tr = cornersList.contains(.topRight) ? radius : 0
        let bl = cornersList.contains(.bottomLeft) ? radius : 0
        let br = cornersList.contains(.bottomRight) ? radius : 0
        
        let minx = rect.minX
        let miny = rect.minY
        let maxx = rect.maxX
        let maxy = rect.maxY
        
        path.move(to: CGPoint(x: minx + tl, y: miny))
        path.addLine(to: CGPoint(x: maxx - tr, y: miny))
        path.addArc(center: CGPoint(x: maxx - tr, y: miny + tr), radius: tr, startAngle: Angle(degrees: -90), endAngle: Angle(degrees: 0), clockwise: false)
        path.addLine(to: CGPoint(x: maxx, y: maxy - br))
        path.addArc(center: CGPoint(x: maxx - br, y: maxy - br), radius: br, startAngle: Angle(degrees: 0), endAngle: Angle(degrees: 90), clockwise: false)
        path.addLine(to: CGPoint(x: minx + bl, y: maxy))
        path.addArc(center: CGPoint(x: minx + bl, y: maxy - bl), radius: bl, startAngle: Angle(degrees: 90), endAngle: Angle(degrees: 180), clockwise: false)
        path.addLine(to: CGPoint(x: minx, y: miny + tl))
        path.addArc(center: CGPoint(x: minx + tl, y: miny + tl), radius: tl, startAngle: Angle(degrees: 180), endAngle: Angle(degrees: 270), clockwise: false)
        
        return path
    }
}

#Preview {
    AIChatPanelView()
        .frame(width: 800, height: 800)
}
