import SwiftUI

struct AlphaDashboardView: View {
    @State private var viewModel = AlphaStreamViewModel()
    
    var body: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 20) {
                // Header
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("ALPHA COMMAND CENTER")
                            .premiumTypography(.overline)
                            .foregroundStyle(.white.opacity(0.7))
                        
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(String(format: "%.2f%%", viewModel.lastAlpha * 100))
                                .premiumTypography(.heading)
                                .foregroundStyle(viewModel.lastAlpha >= 0 ? Color.stitchNeonGreen : .growinRed)
                            
                            Text("vs FTSE 100")
                                .premiumTypography(.caption)
                                .foregroundStyle(.white.opacity(0.4))
                        }
                    }
                    
                    Spacer()
                    
                    RegimeIndicatorView(regime: viewModel.currentRegime)
                }
                
                // High-Performance Canvas
                GeometryReader { geo in
                    TimelineView(.animation) { timeline in
                        Canvas { context, size in
                            let points = viewModel.displayPoints
                            guard points.count >= 2 else {
                                context.draw(Text("AWAITING ALPHA STREAM...").premiumTypography(.caption), at: CGPoint(x: size.width/2, y: size.height/2))
                                return
                            }
                            
                            // 1. Draw Grid Lines (Background)
                            drawGrid(context: context, size: size)
                            
                            // 2. Draw Benchmark Line (FTSE 100)
                            var benchmarkPath = Path()
                            benchmarkPath.move(to: CGPoint(x: points[0].x, y: points[0].yBenchmark))
                            
                            // 3. Draw Portfolio Line (Growin)
                            var portfolioPath = Path()
                            portfolioPath.move(to: CGPoint(x: points[0].x, y: points[0].yPortfolio))
                            
                            for i in 1..<points.count {
                                benchmarkPath.addLine(to: CGPoint(x: points[i].x, y: points[i].yBenchmark))
                                portfolioPath.addLine(to: CGPoint(x: points[i].x, y: points[i].yPortfolio))
                            }
                            
                            // Render Benchmark
                            context.stroke(
                                benchmarkPath,
                                with: .color(.white.opacity(0.15)),
                                style: StrokeStyle(lineWidth: 1.5, lineCap: .round, dash: [5, 5])
                            )
                            
                            // Render Portfolio with Glow
                            context.addFilter(.shadow(color: Color.stitchNeonGreen.opacity(0.5), radius: 5))
                            context.stroke(
                                portfolioPath,
                                with: .linearGradient(
                                    Gradient(colors: [Color.stitchNeonGreen.opacity(0.8), Color.stitchNeonGreen]),
                                    startPoint: .zero,
                                    endPoint: CGPoint(x: size.width, y: size.height)
                                ),
                                style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round)
                            )
                        }
                    }
                    .onChange(of: geo.size) { _, newSize in
                        Task {
                            await viewModel.updateDisplayCoordinates(size: newSize)
                        }
                    }
                    .onAppear {
                        Task {
                            await viewModel.updateDisplayCoordinates(size: geo.size)
                        }
                    }
                }
                .frame(height: 220)
                .drawingGroup() // Force Metal-accelerated layer
                
                // Footer Metrics
                HStack(spacing: 24) {
                    MetricMiniLabel(title: "LATENCY", value: "14ms", color: .stitchNeonGreen)
                    MetricMiniLabel(title: "PRECISION", value: "SOTA", color: .white.opacity(0.6))
                    MetricMiniLabel(title: "ENGINE", value: "Metal v3", color: .white.opacity(0.6))
                }
            }
            .padding(24)
        }
        .onAppear {
            viewModel.connect()
        }
        .onDisappear {
            viewModel.disconnect()
        }
    }
    
    private func drawGrid(context: GraphicsContext, size: CGSize) {
        let lines = 5
        for i in 0...lines {
            let y = CGFloat(i) * (size.height / CGFloat(lines))
            var path = Path()
            path.move(to: CGPoint(x: 0, y: y))
            path.addLine(to: CGPoint(x: size.width, y: y))
            context.stroke(path, with: .color(.white.opacity(0.05)), lineWidth: 1)
        }
    }
}

struct MetricMiniLabel: View {
    let title: String
    let value: String
    let color: Color
    
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 7, weight: .black, design: .monospaced))
                .foregroundStyle(.white.opacity(0.4))
            Text(value)
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundStyle(color)
        }
    }
}
