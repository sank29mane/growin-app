import SwiftUI
import Charts

/// StrategyLabView: Sovereign Alpha hyperparameter calibration lab.
/// Provides visual feedback for PPO tuning with brutal editorial aesthetics.
struct StrategyLabView: View {
    @State private var learningRate: Double = 3e-4
    @State private var gamma: Double = 0.99
    @State private var gaeLambda: Double = 0.95
    @State private var clipRange: Double = 0.2
    
    // Mock data for tuning graph
    @State private var performanceHistory: [TuningPoint] = TuningPoint.mock
    
    var body: some View {
        SovereignContainer {
            VStack(alignment: .leading, spacing: 0) {
                // Header
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("STRATEGY CALIBRATION")
                            .font(SovereignTheme.Fonts.notoSerif(size: 24))
                            .foregroundStyle(Color.brutalOffWhite)
                        
                        Text("PPO ENGINE STABILITY VERIFICATION // PHASE 40")
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                            .foregroundStyle(Color.brutalChartreuse)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 4) {
                        Text("CALIBRATION_READY")
                            .font(SovereignTheme.Fonts.monacoTechnical(size: 9))
                            .foregroundStyle(Color.brutalChartreuse)
                        
                        Text("SOTERIA_NODE_01")
                            .font(SovereignTheme.Fonts.monacoTechnical(size: 9))
                            .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                    }
                }
                .padding()
                
                Divider()
                    .background(Color.white.opacity(0.15))
                
                ScrollView {
                    VStack(alignment: .leading, spacing: 32) {
                        // 1. Tuning Graph
                        VStack(alignment: .leading, spacing: 12) {
                            Text("REWARD IMPACT PROJECTION")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                            
                            SovereignCard {
                                Chart {
                                    ForEach(performanceHistory) { point in
                                        LineMark(
                                            x: .value("Step", point.step),
                                            y: .value("Reward", point.reward)
                                        )
                                        .interpolationMethod(.monotone)
                                        .foregroundStyle(Color.brutalChartreuse)
                                        
                                        AreaMark(
                                            x: .value("Step", point.step),
                                            y: .value("Reward", point.reward)
                                        )
                                        .interpolationMethod(.monotone)
                                        .foregroundStyle(
                                            LinearGradient(
                                                colors: [Color.brutalChartreuse.opacity(0.1), .clear],
                                                startPoint: .top,
                                                endPoint: .bottom
                                            )
                                        )
                                    }
                                }
                                .chartYAxis {
                                    AxisMarks(position: .leading) { value in
                                        AxisValueLabel {
                                            if let doubleValue = value.as(Double.self) {
                                                Text("\(doubleValue, specifier: "%.1f")")
                                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                                            }
                                        }
                                        AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [2, 2]))
                                            .foregroundStyle(Color.white.opacity(0.1))
                                    }
                                }
                                .chartXAxis {
                                    AxisMarks { value in
                                        AxisValueLabel {
                                            if let intValue = value.as(Int.self) {
                                                Text("\(intValue)")
                                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                                            }
                                        }
                                    }
                                }
                                .frame(height: 180)
                            }
                        }
                        .padding(.horizontal)
                        
                        // 2. Hyperparameter Controls
                        VStack(alignment: .leading, spacing: 20) {
                            Text("PPO HYPERPARAMETERS")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                            
                            VStack(spacing: 24) {
                                ParameterSlider(
                                    label: "LEARNING RATE (η)",
                                    value: $learningRate,
                                    range: 1e-5...1e-3,
                                    format: "%.5f"
                                )
                                
                                ParameterSlider(
                                    label: "DISCOUNT FACTOR (γ)",
                                    value: $gamma,
                                    range: 0.8...0.999,
                                    format: "%.3f"
                                )
                                
                                ParameterSlider(
                                    label: "GAE LAMBDA (λ)",
                                    value: $gaeLambda,
                                    range: 0.9...1.0,
                                    format: "%.2f"
                                )
                                
                                ParameterSlider(
                                    label: "CLIP RANGE (ε)",
                                    value: $clipRange,
                                    range: 0.1...0.3,
                                    format: "%.2f"
                                )
                            }
                            .padding()
                            .background(Color.brutalRecessed)
                            .border(Color.white.opacity(0.1), width: 1)
                        }
                        .padding(.horizontal)
                        
                        // 3. Status Action
                        Button(action: {
                            // Initiation logic
                        }) {
                            Text("INITIATE AGENT RE-TRAINING")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                                .frame(maxWidth: .infinity)
                        }
                        .sovereignButtonStyle()
                        .padding(.horizontal)
                        .padding(.bottom, 40)
                    }
                    .padding(.top)
                }
            }
        }
    }
}

private struct ParameterSlider: View {
    let label: String
    @Binding var value: Double
    let range: ClosedRange<Double>
    let format: String
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(label)
                    .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                    .foregroundStyle(Color.brutalOffWhite)
                
                Spacer()
                
                Text(String(format: format, value))
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .foregroundStyle(Color.brutalChartreuse)
            }
            
            Slider(value: $value, in: range)
                .tint(Color.brutalChartreuse)
        }
    }
}

private struct TuningPoint: Identifiable {
    let id = UUID()
    let step: Int
    let reward: Double
    
    static var mock: [TuningPoint] {
        (0...20).map { i in
            TuningPoint(step: i, reward: 2.0 + sin(Double(i) * 0.5) + Double(i) * 0.1)
        }
    }
}

#Preview {
    StrategyLabView()
        .preferredColorScheme(.dark)
}
