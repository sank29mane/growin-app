import SwiftUI

struct RegimeIndicatorView: View {
    let regime: String
    
    var regimeStatus: (color: Color, label: String, description: String) {
        switch regime.lowercased() {
        case "bull", "calm":
            return (.stitchNeonGreen, "CALM", "Steady Alpha")
        case "sideways", "dynamic":
            return (.yellow, "DYNAMIC", "Volatility Rising")
        case "bear", "crisis":
            return (.growinRed, "CRISIS", "Protection Active")
        case "offline":
            return (.gray, "OFFLINE", "Stream Interrupted")
        default:
            return (.indigo, "ALPHA", "Synchronizing")
        }
    }
    
    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle()
                    .fill(regimeStatus.color.opacity(0.3))
                    .frame(width: 14, height: 14)
                    .blur(radius: 4)
                
                Circle()
                    .fill(regimeStatus.color)
                    .frame(width: 8, height: 8)
            }
            .animation(.easeInOut(duration: 1).repeatForever(), value: regime)
            
            VStack(alignment: .leading, spacing: 0) {
                Text(regimeStatus.label)
                    .premiumTypography(.overline)
                    .foregroundStyle(regimeStatus.color)
                
                Text(regimeStatus.description)
                    .font(.system(size: 8, weight: .bold, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.6))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            Capsule()
                .fill(Color.black.opacity(0.4))
                .overlay(
                    Capsule()
                        .stroke(regimeStatus.color.opacity(0.3), lineWidth: 1)
                )
        )
    }
}
