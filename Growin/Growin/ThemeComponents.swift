import SwiftUI

struct GlassCard<Content: View>: View {
    let content: Content
    let cornerRadius: CGFloat
    
    init(cornerRadius: CGFloat = 16, @ViewBuilder content: () -> Content) {
        self.cornerRadius = cornerRadius
        self.content = content()
    }
    
    var body: some View {
        ZStack {
            // Background Layer
            RoundedRectangle(cornerRadius: cornerRadius)
                .fill(Color.glassBackground) // Fallback/Tint
                .background(.ultraThinMaterial) // The Blur
                .cornerRadius(cornerRadius)
            
            // Content Layer - Explicitly on top
            content
                .padding()
        }
        .overlay(
            RoundedRectangle(cornerRadius: cornerRadius)
                .stroke(Color.glassBorder, lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.3), radius: 10, x: 0, y: 5)
    }
}

struct MarkdownText: View {
    let content: String
    
    var body: some View {
        // In SwiftUI on macOS, Text(LocalizedStringKey(content)) provides basic markdown support
        Text(LocalizedStringKey(content))
            .textSelection(.enabled)
    }
}

struct PersonaIcon: View {
    let name: String
    
    var body: some View {
        Image(systemName: iconName)
            .foregroundStyle(iconColor)
            .font(.system(size: 14, weight: .bold))
            .accessibilityLabel(name)
    }
    
    private var iconName: String {
        switch name {
        case "Portfolio Analyst": return "chart.pie.fill"
        case "Risk Manager": return "shield.fill"
        case "Technical Trader": return "chart.line.uptrend.xyaxis"
        case "Execution Specialist": return "bolt.fill"
        default: return "brain.head.profile"
        }
    }
    
    private var iconColor: Color {
        switch name {
        case "Portfolio Analyst": return Color.Persona.analyst
        case "Risk Manager": return Color.Persona.risk
        case "Technical Trader": return Color.Persona.trader
        case "Execution Specialist": return Color.Persona.execution
        default: return .white
        }
    }
}

struct GradientBackground: View {
    var body: some View {
        LinearGradient(
            colors: [
                Color.black,
                Color(red: 0.05, green: 0.08, blue: 0.15)
            ],
            startPoint: .top,
            endPoint: .bottom
        )
        .ignoresSafeArea()
    }
}

struct AppHeader: View {
    let title: String
    let subtitle: String
    var icon: String? = nil
    
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 6) {
                Text(title)
                    .font(.system(size: 28, weight: .black))
                    .foregroundStyle(.white)
                Text(subtitle)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundStyle(.white.opacity(0.5))
            }
            Spacer()
            
            if let icon = icon {
                ZStack {
                    Circle()
                        .fill(Color.blue.opacity(0.15))
                        .frame(width: 44, height: 44)
                    Image(systemName: icon)
                        .font(.system(size: 20))
                        .foregroundStyle(.blue)
                }
            }
        }
    }
}

// MARK: - Utility Components

struct ErrorCard: View {
    let message: String
    var retryAction: (() -> Void)? = nil
    
    var body: some View {
        GlassCard(cornerRadius: 16) {
            HStack(spacing: 16) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 24))
                    .foregroundStyle(.red)
                
                VStack(alignment: .leading, spacing: 4) {
                    Text("Something went wrong")
                        .font(.system(size: 14, weight: .bold))
                    Text(message)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
                
                Spacer()
                
                if let retry = retryAction {
                    Button("Retry", action: retry)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }
        }
    }
}

struct LargeHeadline: View {
    let text: String
    
    var body: some View {
        Text(text)
            .font(.system(size: 32, weight: .black, design: .rounded))
            .foregroundStyle(.white)
    }
}
