import SwiftUI
import Combine

struct PerformanceMetricsOverlay: View {
    @State private var fps: Int = 120
    @State private var hitchRate: Double = 0.0
    
    let timer = Timer.publish(every: 1, on: .main, in: .common).autoconnect()
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Circle()
                    .fill(fps >= 115 ? Color.stitchNeonGreen : .growinRed)
                    .frame(width: 8, height: 8)
                
                Text("\(fps) FPS")
                    .font(.system(.caption, design: .monospaced))
                    .bold()
            }
            
            Text("HITCH: \(String(format: "%.1f", hitchRate))ms")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(8)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.white.opacity(0.1), lineWidth: 0.5)
        )
        .onReceive(timer) { _ in
            // Simulation of performance jitter for demo
            fps = Int.random(in: 118...120)
            hitchRate = Double.random(in: 0...0.5)
        }
    }
}
