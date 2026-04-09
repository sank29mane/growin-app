import SwiftUI

/// AIChatPanelView: The conversational hub for the Sovereign Desktop UI.
/// Incorporates rounded "Mac-native" bubble aesthetics while retaining the core dark-theme color palette (Brutal Chartreuse, Cyan).
struct AIChatPanelView: View {
    @State private var chatInput: String = ""
    @State private var selectedFilter: AccountFilter = .all
    
    // State to simulate switching into an active chat view vs the "Home" state
    @State private var hasStartedChat: Bool = false
    @State private var chatMessages: [ChatMessage] = []
    
    enum AccountFilter: String, CaseIterable {
        case all = "All Accounts"
        case isa = "ISA"
        case invest = "Invest"
        
        var icon: String {
            switch self {
            case .all: return "globe.europe.africa.fill"
            case .isa: return "star.fill"
            case .invest: return "chart.line.uptrend.xyaxis"
            }
        }
    }
    
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
                    ActiveChatView(messages: $chatMessages)
                } else {
                    // Discovery Home Dashboard
                    Spacer()
                    DiscoveryHomeView(onTileTap: { prompt in
                        startChat(with: prompt)
                    })
                    Spacer()
                }
                
                // Bottom Input Area
                VStack(spacing: 12) {
                    // Account Filter Pills
                    HStack(spacing: 8) {
                        ForEach(AccountFilter.allCases, id: \.self) { filter in
                            Button(action: { selectedFilter = filter }) {
                                HStack(spacing: 6) {
                                    if filter == .all {
                                        Image(systemName: "brain.filled.head.profile")
                                            .font(.system(size: 10))
                                    } else {
                                        Image(systemName: filter.icon)
                                            .font(.system(size: 10))
                                    }
                                    Text(filter.rawValue)
                                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 11, weight: .bold))
                                }
                                .padding(.horizontal, 16)
                                .padding(.vertical, 8)
                                .background(selectedFilter == filter ? Color.cyan : Color.white.opacity(0.05))
                                .foregroundStyle(selectedFilter == filter ? Color.black : Color.brutalOffWhite)
                                .clipShape(Capsule())
                                .overlay(
                                    Capsule()
                                        .stroke(selectedFilter == filter ? Color.clear : Color.white.opacity(0.1), lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Filter by \(filter.rawValue)")
                            .accessibilityAddTraits(selectedFilter == filter ? [.isButton, .isSelected] : [.isButton])
                        }
                        
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
                        .accessibilityLabel("Send message")
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
        withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
            hasStartedChat = true
            chatMessages.append(ChatMessage(id: UUID(), text: prompt, isUser: true))
            
            // Mock System Response containing dynamic tiles/data
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
                withAnimation {
                    chatMessages.append(ChatMessage(id: UUID(), text: "Analyzing your portfolio strategy. Here's what I found for the selected instrument.", isUser: false, hasDynamicTiles: true))
                }
            }
        }
    }
}

// MARK: - Discovery Home View
private struct DiscoveryHomeView: View {
    let onTileTap: (String) -> Void
    
    // Dynamic exploration prompts
    let tiles: [(title: String, icon: String, iconColor: Color)] = [
        ("Portfolio Overview", "chart.bar.xaxis", .green),
        ("Tomorrow's Plays", "target", .red),
        ("ISA Account", "chart.line.uptrend.xyaxis", .green),
        ("Invest Account", "bag.fill", .yellow),
        ("Risk Check", "exclamationmark.triangle.fill", .orange),
        ("Market Outlook", "chart.xyaxis.line", .cyan)
    ]
    
    let columns = [
        GridItem(.flexible(), spacing: 16),
        GridItem(.flexible(), spacing: 16)
    ]
    
    var body: some View {
        VStack(spacing: 40) {
            // Hero Branding
            VStack(spacing: 16) {
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 56, weight: .light))
                    .foregroundStyle(Color.cyan)
                    .shadow(color: Color.cyan.opacity(0.6), radius: 20, x: 0, y: 0) // Glowing effect without purple
                
                VStack(spacing: 8) {
                    Text("Growin AI Trading")
                        .font(SovereignTheme.Fonts.notoSerif(size: 28, weight: .bold))
                        .foregroundStyle(Color.brutalOffWhite)
                    
                    Text("Your intelligent trading companion")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                }
            }
            
            VStack(spacing: 24) {
                Text("What would you like to explore?")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                
                LazyVGrid(columns: columns, spacing: 16) {
                    ForEach(tiles, id: \.title) { tile in
                        Button(action: { onTileTap(tile.title) }) {
                            HStack(spacing: 12) {
                                Image(systemName: tile.icon)
                                    .font(.system(size: 14))
                                    .foregroundStyle(tile.iconColor)
                                
                                Text(tile.title)
                                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                                    .foregroundStyle(Color.brutalOffWhite)
                                
                                Spacer()
                                
                                Image(systemName: "arrow.right.circle.fill")
                                    .foregroundStyle(Color.white.opacity(0.2))
                            }
                            .padding(16)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.white.opacity(0.04))
                            .clipShape(RoundedRectangle(cornerRadius: 12)) // Mac-native rounded style
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .stroke(Color.white.opacity(0.08), lineWidth: 1)
                            )
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Explore \(tile.title)")
                        .accessibilityHint("Starts a conversation about \(tile.title)")
                        .accessibilityAddTraits(.isButton)
                        // Simple hover mechanic simulator
                        .onHover { isHovered in
                            guard isHovered else { return }
                            NSCursor.pointingHand.push()
                        }
                    }
                }
                .frame(maxWidth: 600)
            }
        }
    }
}

// MARK: - Active Chat View
private struct ActiveChatView: View {
    @Binding var messages: [ChatMessage]
    
    var body: some View {
        ScrollView {
            LazyVStack(spacing: 24) {
                ForEach(messages) { message in
                    ChatMessageRow(message: message)
                }
            }
            .padding(24)
        }
    }
}

private struct ChatMessageRow: View {
    let message: ChatMessage
    
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
        .accessibilityLabel(title)
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
