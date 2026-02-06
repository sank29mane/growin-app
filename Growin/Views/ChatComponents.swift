import SwiftUI
import Combine

// MARK: - Models

struct SuggestionItem: Identifiable {
    let id = UUID()
    let icon: String
    let title: String
    let prompt: String
    let color: Color
}

struct QuickAction: Identifiable, Codable {
    var id: String { label }
    let icon: String
    let label: String
    let prompt: String
}

// MARK: - Welcome View

struct WelcomeView: View {
    let onSuggestionTap: (String) -> Void
    @State private var appearAnimation = false
    
    private let suggestions = [
        SuggestionItem(icon: "📊", title: "Portfolio Overview", prompt: "Give me an overview of my portfolio performance", color: .blue),
        SuggestionItem(icon: "🎯", title: "Tomorrow's Plays", prompt: "What trading opportunities do you see for tomorrow?", color: .orange),
        SuggestionItem(icon: "📈", title: "ISA Account", prompt: "How is my ISA account performing?", color: .green),
        SuggestionItem(icon: "💰", title: "Invest Account", prompt: "Analyze my Invest account", color: .purple),
        SuggestionItem(icon: "⚠️", title: "Risk Check", prompt: "Evaluate my portfolio risk exposure and diversification", color: .red),
        SuggestionItem(icon: "📉", title: "Market Outlook", prompt: "What's the market outlook for this week?", color: .cyan)
    ]
    
    var body: some View {
        VStack(spacing: 32) {
            Spacer()
            
            // Header with animated icon
            VStack(spacing: 16) {
                ZStack {
                    // Glow effect
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [Color.blue.opacity(0.3), Color.clear],
                                center: .center,
                                startRadius: 30,
                                endRadius: 80
                            )
                        )
                        .frame(width: 120, height: 120)
                        .blur(radius: 10)
                    
                    // Icon
                    Image(systemName: "brain.head.profile")
                        .font(.system(size: 56, weight: .light))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [.blue, .purple],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .scaleEffect(appearAnimation ? 1.0 : 0.8)
                        .opacity(appearAnimation ? 1.0 : 0.0)
                }
                
                VStack(spacing: 6) {
                    Text("Growin AI Trading")
                        .font(.system(size: 28, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                    
                    Text("Your intelligent trading companion")
                        .font(.system(size: 15))
                        .foregroundStyle(.secondary)
                }
                .opacity(appearAnimation ? 1.0 : 0.0)
                .offset(y: appearAnimation ? 0 : 10)
            }
            
            // Suggestion Chips Grid
            VStack(spacing: 12) {
                Text("What would you like to explore?")
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(.secondary)
                    .opacity(appearAnimation ? 1.0 : 0.0)
                
                LazyVGrid(columns: [
                    GridItem(.flexible(), spacing: 12),
                    GridItem(.flexible(), spacing: 12)
                ], spacing: 12) {
                    ForEach(Array(suggestions.enumerated()), id: \.element.id) { index, item in
                        SuggestionChip(item: item) {
                            onSuggestionTap(item.prompt)
                        }
                        .opacity(appearAnimation ? 1.0 : 0.0)
                        .offset(y: appearAnimation ? 0 : 20)
                        .animation(
                            .spring(response: 0.5, dampingFraction: 0.8)
                            .delay(Double(index) * 0.08),
                            value: appearAnimation
                        )
                    }
                }
                .padding(.horizontal, 20)
            }
            
            Spacer()
            
            // Hint text
            Text("Or type any question below...")
                .font(.caption)
                .foregroundStyle(.secondary.opacity(0.7))
                .opacity(appearAnimation ? 1.0 : 0.0)
                .padding(.bottom, 8)
        }
        .frame(maxWidth: 500)
        .onAppear {
            withAnimation(.easeOut(duration: 0.6)) {
                appearAnimation = true
            }
        }
    }
}

// MARK: - Suggestion Chip

struct SuggestionChip: View {
    let item: SuggestionItem
    let action: () -> Void
    @State private var isHovered = false
    @State private var isPressed = false
    
    var body: some View {
        Button(action: {
            withAnimation(.spring(response: 0.2, dampingFraction: 0.6)) {
                isPressed = true
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                isPressed = false
                action()
            }
        }) {
            HStack(spacing: 10) {
                Text(item.icon)
                    .font(.system(size: 20))
                
                Text(item.title)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.white.opacity(0.9))
                
                Spacer()
                
                Image(systemName: "arrow.right.circle.fill")
                    .font(.system(size: 16))
                    .foregroundStyle(item.color.opacity(isHovered ? 0.8 : 0.4))
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity)
            .background(
                ZStack {
                    // Glass effect
                    RoundedRectangle(cornerRadius: 14)
                        .fill(.ultraThinMaterial)
                    
                    // Gradient overlay on hover
                    if isHovered {
                        RoundedRectangle(cornerRadius: 14)
                            .fill(
                                LinearGradient(
                                    colors: [item.color.opacity(0.15), item.color.opacity(0.05)],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                )
                            )
                    }
                }
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(
                        isHovered ? item.color.opacity(0.5) : Color.white.opacity(0.1),
                        lineWidth: 1
                    )
            )
            .scaleEffect(isPressed ? 0.97 : (isHovered ? 1.02 : 1.0))
            .shadow(
                color: isHovered ? item.color.opacity(0.2) : .clear,
                radius: 12,
                y: 4
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Ask about \(item.title)")
        .accessibilityHint("Asks: \(item.prompt)")

        .onHover { hovering in
            withAnimation(.easeOut(duration: 0.15)) {
                isHovered = hovering
            }
        }
    }
}

// MARK: - Account Picker

struct AccountPicker: View {
    @Binding var selectedAccount: String
    private let accounts = ["all", "isa", "invest"]
    
    private func displayName(_ account: String) -> String {
        switch account {
        case "all": return "All Accounts"
        case "isa": return "ISA"
        case "invest": return "Invest"
        default: return account.capitalized
        }
    }
    
    private func icon(_ account: String) -> String {
        switch account {
        case "all": return "square.stack.3d.up.fill"
        case "isa": return "building.columns.fill"
        case "invest": return "chart.line.uptrend.xyaxis"
        default: return "circle"
        }
    }
    
    var body: some View {
        HStack(spacing: 6) {
            ForEach(accounts, id: \.self) { account in
                AccountPickerButton(
                    account: account,
                    isSelected: selectedAccount == account,
                    displayName: displayName(account),
                    icon: icon(account)
                ) {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                        selectedAccount = account
                    }
                }
            }
        }
        .padding(6)
        .background(
            Capsule()
                .fill(Color.black.opacity(0.3))
        )
    }
}

private struct AccountPickerButton: View {
    let account: String
    let isSelected: Bool
    let displayName: String
    let icon: String
    let action: () -> Void
    
    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 10))
                Text(displayName)
                    .font(.system(size: 12, weight: .medium))
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(backgroundView)
            .overlay(overlayView)
            .foregroundStyle(isSelected ? .white : .secondary)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(displayName)
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
        .accessibilityHint("Filters chat context to \(displayName)")
    }
    
    @ViewBuilder
    private var backgroundView: some View {
        if isSelected {
            Capsule()
                .fill(
                    LinearGradient(
                        colors: [.blue, .blue.opacity(0.8)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
        } else {
            Capsule()
                .fill(
                    LinearGradient(
                        colors: [Color.white.opacity(0.08), Color.white.opacity(0.05)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )
        }
    }
    
    @ViewBuilder
    private var overlayView: some View {
        Capsule()
            .stroke(
                isSelected ? Color.blue.opacity(0.3) : Color.white.opacity(0.1),
                lineWidth: 1
            )
    }
}

// MARK: - Quick Action Buttons (for responses)

struct QuickActionButtons: View {
    let actions: [QuickAction]
    let onTap: (String) -> Void
    
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(actions) { action in
                    Button(action: { onTap(action.prompt) }) {
                        HStack(spacing: 6) {
                            Text(action.icon)
                                .font(.system(size: 12))
                            Text(action.label)
                                .font(.system(size: 12, weight: .medium))
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(
                            Capsule()
                                .fill(Color.blue.opacity(0.15))
                        )
                        .overlay(
                            Capsule()
                                .stroke(Color.blue.opacity(0.3), lineWidth: 1)
                        )
                        .foregroundStyle(.blue)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(action.label)
                    .accessibilityHint("Asks: \(action.prompt)")
                }
            }
        }
        .padding(.top, 8)
    }
}

// MARK: - Enhanced Typing Indicator

struct EnhancedTypingIndicator: View {
    var statusText: String = "Analyzing your request..."
    @State private var dotIndex = 0
    @State private var pulseScale: CGFloat = 1.0
    
    private let timer = Timer.publish(every: 0.4, on: .main, in: .common).autoconnect()
    
    var body: some View {
        HStack(spacing: 14) {
            // Animated brain icon
            ZStack {
                Circle()
                    .fill(Color.blue.opacity(0.2))
                    .frame(width: 36, height: 36)
                    .scaleEffect(pulseScale)
                
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 18))
                    .foregroundStyle(.blue)
            }
            
            VStack(alignment: .leading, spacing: 6) {
                // Animated dots
                HStack(spacing: 4) {
                    ForEach(0..<3) { i in
                        Circle()
                            .fill(Color.blue)
                            .frame(width: 7, height: 7)
                            .scaleEffect(dotIndex == i ? 1.3 : 1.0)
                            .opacity(dotIndex == i ? 1.0 : 0.4)
                    }
                }
                
                // Status text
                Text(statusText)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(statusText)
        .onReceive(timer) { _ in
            withAnimation(.easeInOut(duration: 0.3)) {
                dotIndex = (dotIndex + 1) % 3
                pulseScale = pulseScale == 1.0 ? 1.1 : 1.0
            }
        }
    }
}

// MARK: - Preview

#Preview("Welcome View") {
    ZStack {
        Color.black.ignoresSafeArea()
        WelcomeView { prompt in
            print("Selected: \(prompt)")
        }
    }
}

#Preview("Account Picker") {
    ZStack {
        Color.black.ignoresSafeArea()
        VStack {
            AccountPicker(selectedAccount: .constant("all"))
        }
    }
}

#Preview("Typing Indicator") {
    ZStack {
        Color.black.ignoresSafeArea()
        EnhancedTypingIndicator(statusText: "Fetching portfolio data...")
            .padding()
    }
}
