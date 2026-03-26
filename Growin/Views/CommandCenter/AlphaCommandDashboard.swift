import SwiftUI
import Combine

/// AlphaCommandDashboard: The primary 120Hz Alpha tracking dashboard.
/// Uses SwiftUI.Canvas for high-performance rendering of streaming Alpha data.
struct AlphaCommandDashboard: View {
    @StateObject private var viewModel = AlphaCommandViewModel()
    
    var body: some View {
        SovereignContainer {
            VStack(alignment: .leading, spacing: 0) {
                // Header
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("ALPHA COMMAND")
                            .sovereignHeader(size: 24)
                        Text("STRATEGY: PPO-CONV-01")
                            .sovereignTechnical(size: 12)
                            .opacity(0.6)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 4) {
                        Text("REWARD")
                            .sovereignTechnical(size: 10)
                            .opacity(0.4)
                        Text(String(format: "+%.4f", viewModel.currentAlpha))
                            .sovereignTechnical(size: 20)
                            .acidAccent()
                    }
                }
                .padding(24)
                
                // 120Hz Alpha Stream Tracker
                AlphaStreamTracker(data: viewModel.alphaHistory)
                    .equatable() // Optimization to skip redraws if data is same
                    .frame(maxWidth: .infinity)
                    .frame(height: 200)
                    .padding(.horizontal, 24)
                
                // Dashboard Grid (Placeholder for AlphaLedgerView integration)
                AlphaLedgerView()
                    .padding(24)
                
                Spacer()
            }
        }
    }
}

/// A high-performance 120Hz Alpha chart using SwiftUI.Canvas.
struct AlphaStreamTracker: View, Equatable {
    let data: [AlphaDataPoint]
    
    static func == (lhs: AlphaStreamTracker, rhs: AlphaStreamTracker) -> Bool {
        // Skip redraw if data has not changed significantly
        guard lhs.data.count == rhs.data.count else { return false }
        return lhs.data.last?.id == rhs.data.last?.id
    }
    
    var body: some View {
        Canvas { context, size in
            guard data.count > 1 else { return }
            
            let values = data.map { $0.value }
            let maxAlpha = values.max() ?? 1.0
            let minAlpha = values.min() ?? 0.0
            let range = maxAlpha - minAlpha
            let stepX = size.width / CGFloat(data.count - 1)
            
            var path = Path()
            for (index, point) in data.enumerated() {
                let x = CGFloat(index) * stepX
                let normalizedY = range > 0 ? (point.value - minAlpha) / range : 0.5
                let y = size.height - (normalizedY * size.height)
                
                if index == 0 {
                    path.move(to: CGPoint(x: x, y: y))
                } else {
                    path.addLine(to: CGPoint(x: x, y: y))
                }
            }
            
            // Draw gradient fill
            var fillPath = path
            fillPath.addLine(to: CGPoint(x: size.width, y: size.height))
            fillPath.addLine(to: CGPoint(x: 0, y: size.height))
            fillPath.closeSubpath()
            
            context.fill(
                fillPath,
                with: .linearGradient(
                    Gradient(colors: [SovereignTheme.Colors.brutalChartreuse.opacity(0.15), .clear]),
                    startPoint: .zero,
                    endPoint: CGPoint(x: 0, y: size.height)
                )
            )
            
            // Draw line
            context.stroke(
                path,
                with: .color(SovereignTheme.Colors.brutalChartreuse),
                lineWidth: 1.5
            )
        }
        .drawingGroup() // Offload to Metal for 120Hz performance
    }
}

struct AlphaDataPoint: Equatable, Identifiable {
    let id = UUID()
    let timestamp: Double
    let value: Double
}

class AlphaCommandViewModel: ObservableObject {
    @Published var alphaHistory: [AlphaDataPoint] = []
    @Published var currentAlpha: Double = 0.0
    
    private var timer: Timer?
    
    init() {
        generateInitialData()
        startStreaming()
    }
    
    private func generateInitialData() {
        var points: [AlphaDataPoint] = []
        let now = Date().timeIntervalSince1970
        for i in 0..<100 {
            points.append(AlphaDataPoint(
                timestamp: now - Double(100 - i),
                value: 0.5 + sin(Double(i) * 0.1) * 0.2 + Double.random(in: -0.02...0.02)
            ))
        }
        self.alphaHistory = points
        self.currentAlpha = points.last?.value ?? 0.0
    }
    
    private func startStreaming() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let lastValue = self.alphaHistory.last?.value ?? 0.5
            let newValue = lastValue + Double.random(in: -0.01...0.012)
            let newPoint = AlphaDataPoint(timestamp: Date().timeIntervalSince1970, value: newValue)
            
            DispatchQueue.main.async {
                self.alphaHistory.append(newPoint)
                if self.alphaHistory.count > 200 {
                    self.alphaHistory.removeFirst()
                }
                self.currentAlpha = newValue
            }
        }
    }
    
    deinit {
        timer?.invalidate()
    }
}
