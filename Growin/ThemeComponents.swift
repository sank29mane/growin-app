import SwiftUI

// MARK: - Typography System

struct PremiumTypography: ViewModifier {
    enum Style {
        case heading, title, body, caption, overline
    }
    
    let style: Style
    
    func body(content: Content) -> some View {
        switch style {
        case .heading:
            content
                .font(.system(size: 34, weight: .bold, design: .rounded))
                .tracking(-0.5)
        case .title:
            content
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .tracking(-0.2)
        case .body:
            content
                .font(.system(size: 15, weight: .medium, design: .rounded))
                .tracking(0)
        case .caption:
            content
                .font(.system(size: 13, weight: .regular, design: .rounded))
                .foregroundStyle(Color.textSecondary)
        case .overline:
            content
                .font(.system(size: 11, weight: .black, design: .rounded))
                .tracking(1.5)
                .textCase(.uppercase)
                .foregroundStyle(Color.textSecondary)
        }
    }
}

extension View {
    func premiumTypography(_ style: PremiumTypography.Style) -> some View {
        modifier(PremiumTypography(style: style))
    }
}

struct MeshBackground: View {
    @State private var animate = false
    
    var body: some View {
        ZStack {
            Color.growinDarkBg
            
            // Floating Mesh Blobs (Stitch Palette)
            Group {
                Circle()
                    .fill(Color.stitchNeonIndigo.opacity(0.3))
                    .frame(width: 600, height: 600)
                    .offset(x: animate ? -150 : 150, y: animate ? 150 : -150)
                    .blur(radius: 120)
                
                Circle()
                    .fill(Color.stitchNeonCyan.opacity(0.2))
                    .frame(width: 500, height: 500)
                    .offset(x: animate ? 250 : -250, y: animate ? -200 : 200)
                    .blur(radius: 140)
                
                Circle()
                    .fill(Color.stitchNeonPurple.opacity(0.15))
                    .frame(width: 400, height: 400)
                    .offset(x: animate ? -200 : 200, y: animate ? 250 : -250)
                    .blur(radius: 100)
            }
            .opacity(0.6)
            
            // Subtle Noise/Grain
            Rectangle()
                .fill(.ultraThinMaterial)
                .opacity(0.1)
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 20).repeatForever(autoreverses: true)) {
                animate.toggle()
            }
        }
        .ignoresSafeArea()
    }
}

struct GlassCard<Content: View>: View {
    let content: Content
    let cornerRadius: CGFloat
    @State private var isHovered = false
    
    init(cornerRadius: CGFloat = 20, @ViewBuilder content: () -> Content) {
        self.cornerRadius = cornerRadius
        self.content = content()
    }
    
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: cornerRadius)
                .fill(Color.growinSurface.opacity(0.4))
                .background(.ultraThinMaterial)
            
            content
                .padding()
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
        .overlay(
            RoundedRectangle(cornerRadius: cornerRadius)
                .stroke(
                    LinearGradient(
                        colors: [
                            .white.opacity(0.15),
                            .white.opacity(0.02),
                            .white.opacity(0.15)
                        ],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ),
                    lineWidth: 0.5
                )
        )
        .overlay(
            RoundedRectangle(cornerRadius: cornerRadius)
                .stroke(Color.stitchNeonIndigo.opacity(isHovered ? 0.3 : 0), lineWidth: 2)
                .blur(radius: isHovered ? 4 : 0)
        )
        .shadow(color: .black.opacity(0.4), radius: 20, x: 0, y: 15)
        .scaleEffect(isHovered ? 1.015 : 1.0)
        .onHover { hovering in
            withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                isHovered = hovering
            }
        }
    }
}

struct AppHeader: View {
    let title: String
    let subtitle: String
    var icon: String? = nil
    
    var body: some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .premiumTypography(.heading)
                    .foregroundStyle(.white)
                Text(subtitle)
                    .premiumTypography(.body)
                    .foregroundStyle(Color.textSecondary)
            }
            Spacer()
            
            if let icon = icon {
                GlassCard(cornerRadius: 12) {
                    Image(systemName: icon)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Color.growinPrimary)
                }
                .frame(width: 44, height: 44)
            }
        }
        .padding(.vertical)
    }
}

struct PremiumButton: View {
    let title: String
    var icon: String? = nil
    var color: Color = .growinPrimary
    let action: () -> Void
    
    @State private var isPressed = false
    
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if let icon = icon {
                    Image(systemName: icon)
                }
                Text(title)
                    .premiumTypography(.title)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 14)
            .background(
                ZStack {
                    RoundedRectangle(cornerRadius: 14)
                        .fill(color)
                    
                    RoundedRectangle(cornerRadius: 14)
                        .stroke(Color.white.opacity(0.2), lineWidth: 1)
                }
            )
            .foregroundStyle(.white)
            .shadow(color: color.opacity(0.4), radius: 15, x: 0, y: 8)
            .scaleEffect(isPressed ? 0.96 : 1.0)
        }
        .buttonStyle(.plain)
        .simultaneousGesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in withAnimation(.easeOut(duration: 0.1)) { isPressed = true } }
                .onEnded { _ in withAnimation(.spring()) { isPressed = false } }
        )
    }
}

// Reusing Existing Logic for PersonaIcon & MarkdownText with slight style tweaks
struct PersonaIcon: View {
    let name: String
    
    var body: some View {
        Image(systemName: iconName)
            .foregroundStyle(iconColor)
            .font(.system(size: 14, weight: .bold))
            .padding(8)
            .background(iconColor.opacity(0.1))
            .clipShape(Circle())
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

struct MarkdownText: View {
    let content: String
    
    var body: some View {
        Text(LocalizedStringKey(content))
            .premiumTypography(.body)
            .textSelection(.enabled)
    }
}

struct ErrorCard: View {
    let message: String
    let retryAction: () -> Void
    
    var body: some View {
        GlassCard(cornerRadius: 16) {
            VStack(spacing: 16) {
                HStack(spacing: 12) {
                    Image(systemName: "exclamationmark.octagon.fill")
                        .font(.system(size: 24))
                        .foregroundStyle(Color.growinRed)
                    
                    VStack(alignment: .leading, spacing: 4) {
                        Text("SIMULATION ERROR")
                            .premiumTypography(.overline)
                            .foregroundStyle(Color.growinRed)
                        Text(message)
                            .premiumTypography(.body)
                            .foregroundStyle(Color.textSecondary)
                    }
                    Spacer()
                }
                
                Button(action: retryAction) {
                    HStack {
                        Image(systemName: "arrow.clockwise")
                        Text("RETRY PROTOCOL")
                    }
                    .premiumTypography(.overline)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(Color.growinRed.opacity(0.1))
                    .clipShape(Capsule())
                    .foregroundStyle(Color.growinRed)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal)
    }
}

struct GradientBackground: View {
    var body: some View {
        ZStack {
            Color.growinDarkBg
            
            LinearGradient(
                colors: [Color.growinDarkBg, Color.growinSurface.opacity(0.8)],
                startPoint: .top,
                endPoint: .bottom
            )
        }
        .ignoresSafeArea()
    }
}

// MARK: - Safety Components

struct SlideToConfirm: View {
    let title: String
    let action: () -> Void
    
    @State private var offset: CGFloat = 0
    @State private var isConfirmed = false
    private let trackWidth: CGFloat = 300
    private let handleSize: CGFloat = 56
    
    var body: some View {
        ZStack {
            // Track
            Capsule()
                .fill(Color.white.opacity(0.05))
                .frame(width: trackWidth, height: handleSize)
                .overlay(
                    Text(title)
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(.white.opacity(0.3))
                )
            
            // Progress Fill
            HStack {
                Capsule()
                    .fill(Color.growinPrimary)
                    .frame(width: handleSize + offset, height: handleSize)
                Spacer()
            }
            .frame(width: trackWidth)
            
            // Handle
            HStack {
                ZStack {
                    Circle()
                        .fill(.white)
                        .frame(width: handleSize - 8, height: handleSize - 8)
                        .shadow(radius: 5)
                    
                    Image(systemName: isConfirmed ? "checkmark" : "chevron.right.2")
                        .foregroundStyle(Color.growinPrimary)
                        .font(.system(size: 18, weight: .black))
                }
                .offset(x: offset + 4)
                .gesture(
                    DragGesture()
                        .onChanged { value in
                            if !isConfirmed {
                                let newOffset = value.translation.width
                                if newOffset > 0 && newOffset < trackWidth - handleSize {
                                    offset = newOffset
                                }
                            }
                        }
                        .onEnded { value in
                            if !isConfirmed {
                                if offset > trackWidth * 0.7 {
                                    withAnimation(.spring()) {
                                        offset = trackWidth - handleSize
                                        isConfirmed = true
                                    }
                                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                                        action()
                                    }
                                } else {
                                    withAnimation(.spring()) {
                                        offset = 0
                                    }
                                }
                            }
                        }
                )
                Spacer()
            }
            .frame(width: trackWidth)
        }
    }
}

// MARK: - Glass Effect System

enum Glass {
    case thin
    case regular
    case thick
    case ultraThin
    
    var material: Material {
        switch self {
        case .thin: return .thinMaterial
        case .regular: return .regularMaterial
        case .thick: return .thickMaterial
        case .ultraThin: return .ultraThinMaterial
        }
    }
    
    func interactive() -> Glass {
        return self
    }
}

struct GlassEffect: ViewModifier {
    let style: Glass
    
    func body(content: Content) -> some View {
        content
            .background(style.material)
    }
}

extension View {
    func glassEffect(_ style: Glass) -> some View {
        modifier(GlassEffect(style: style))
    }
    
    func glassEffect<S: Shape>(_ style: Glass, in shape: S) -> some View {
        self.background(shape.fill(style.material))
    }
}

// MARK: - New Components from Stitch Designs

enum ConfidenceLevel {
    case high, medium, low
    
    var color: Color {
        switch self {
        case .high: return .stitchNeonGreen
        case .medium: return .stitchNeonYellow
        case .low: return .growinRed
        }
    }
    
    var label: String {
        switch self {
        case .high: return "High"
        case .medium: return "Fair"
        case .low: return "Speculative"
        }
    }
}

struct ConfidenceIndicator: View {
    let score: Double // 0.0 to 1.0
    
    var level: ConfidenceLevel {
        if score >= 0.9 { return .high }
        if score >= 0.7 { return .medium }
        return .low
    }
    
    var body: some View {
        HStack(spacing: 6) {
            Text(level.label.uppercased())
                .premiumTypography(.overline)
                .foregroundStyle(level.color)
            
            Text(String(format: "%.0f%%", score * 100))
                .premiumTypography(.caption)
                .foregroundStyle(.white)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(
            Capsule()
                .fill(level.color.opacity(0.1))
        )
        .overlay(
            Capsule()
                .stroke(
                    level.color.opacity(0.4),
                    style: StrokeStyle(
                        lineWidth: 1,
                        dash: level == .low ? [4] : []
                    )
                )
        )
    }
}

struct ReasoningChip: View {
    let agent: String
    let action: String
    let isActive: Bool
    
    var body: some View {
        HStack(spacing: 8) {
            if isActive {
                ProgressView()
                    .controlSize(.mini)
                    .tint(Color.stitchNeonIndigo)
            } else {
                PersonaIcon(name: agent)
                    .scaleEffect(0.8)
            }
            
            VStack(alignment: .leading, spacing: 0) {
                Text(agent.uppercased())
                    .premiumTypography(.overline)
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                Text(action)
                    .premiumTypography(.caption)
                    .foregroundStyle(.white)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            GlassCard(cornerRadius: 10) { EmptyView() }
                .opacity(isActive ? 1 : 0.6)
        )
    }
}

struct LogicTreeItem: View {
    let title: String
    let content: String
    let isExpanded: Bool
    let onToggle: () -> Void
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(action: onToggle) {
                HStack {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.growinPrimary)
                    
                    Text(title)
                        .premiumTypography(.title)
                        .font(.system(size: 14))
                    
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            
            if isExpanded {
                Text(content)
                    .premiumTypography(.body)
                    .font(.system(size: 13))
                    .foregroundStyle(Color.textSecondary)
                    .padding(.leading, 20)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.02))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}

struct AgentStatusBadge: View {
    let status: String
    let confidence: Double? // 0.0 to 1.0
    
    var body: some View {
        HStack(spacing: 6) {
            if status == "analyzing" || status == "working" {
                Circle()
                    .stroke(statusColor.opacity(0.3), lineWidth: 2)
                    .frame(width: 8, height: 8)
                    .overlay(
                        Circle()
                            .trim(from: 0, to: 0.7)
                            .stroke(statusColor, lineWidth: 2)
                            .rotationEffect(.degrees(360))
                    )
            } else {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
            }
            
            Text(status.uppercased())
                .premiumTypography(.overline)
                .foregroundStyle(statusColor)
            
            if let confidence = confidence {
                Text("|")
                    .foregroundStyle(.white.opacity(0.1))
                Text(String(format: "%.0f%%", confidence * 100))
                    .premiumTypography(.caption)
                    .foregroundStyle(.white.opacity(0.8))
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(
            Capsule()
                .fill(statusColor.opacity(0.1))
        )
        .overlay(
            Capsule()
                .stroke(statusColor.opacity(0.3), lineWidth: 0.5)
        )
    }
    
    private var statusColor: Color {
        switch status.lowercased() {
        case "working", "analyzing": return .orange
        case "ready", "stable": return .stitchNeonGreen
        case "error", "alert": return .growinRed
        case "processing": return .stitchNeonCyan
        default: return .gray
        }
    }
}

struct FinancialMetricView: View {
    let title: String
    let value: String
    let change: String?
    let changePositive: Bool?
    
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .premiumTypography(.overline)
                .foregroundStyle(Color.textSecondary)
            
            Text(value)
                .font(.system(size: 22, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
                .minimumScaleFactor(0.8)
            
            if let change = change, let changePositive = changePositive {
                HStack(spacing: 4) {
                    Image(systemName: changePositive ? "arrow.up.right" : "arrow.down.right")
                    Text(change)
                }
                .premiumTypography(.caption)
                .foregroundStyle(changePositive ? Color.stitchNeonGreen : Color.growinRed)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background((changePositive ? Color.stitchNeonGreen : Color.growinRed).opacity(0.1))
                .clipShape(Capsule())
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 20)
                    .fill(Color.growinSurface.opacity(0.4))
                
                RoundedRectangle(cornerRadius: 20)
                    .stroke(Color.white.opacity(0.05), lineWidth: 1)
            }
        )
    }
}
