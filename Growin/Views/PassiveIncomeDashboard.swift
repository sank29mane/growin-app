import SwiftUI
import Charts

struct PassiveIncomeDashboard: View {
    @StateObject private var viewModel = PassiveIncomeViewModel()
    @State private var selectedTab = 0
    
    var body: some View {
        ZStack {
            MeshBackground()
            
            ScrollView {
                VStack(spacing: 24) {
                    AppHeader(
                        title: "Passive Income",
                        subtitle: "AI-Optimized Yield Engine",
                        icon: "leaf.fill"
                    )
                    .padding(.horizontal)
                    
                    // 1. Core Metrics Summary
                    HStack(spacing: 16) {
                        FinancialMetricView(
                            title: "Settled Income",
                            value: String(format: "$%.2f", viewModel.totalSettled),
                            change: "+12.4%",
                            changePositive: true
                        )
                        
                        FinancialMetricView(
                            title: "Projected (AI)",
                            value: String(format: "$%.2f", viewModel.totalPredicted),
                            change: "Forecast",
                            changePositive: nil
                        )
                    }
                    .padding(.horizontal)
                    
                    // 2. Probability Cloud Forecast Chart
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("YIELD PROBABILITY CLOUD")
                                .premiumTypography(.overline)
                            Spacer()
                            ConfidenceIndicator(score: viewModel.consensus.combined)
                        }
                        .padding(.horizontal)
                        
                        ProbabilityCloudChart(ranges: viewModel.probabilityCloud)
                            .frame(height: 240)
                            .padding(.horizontal)
                    }
                    
                    // 3. HITL Quick-Action Cards
                    if !viewModel.hitlActions.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("PENDING AI RECOMMENDATIONS")
                                .premiumTypography(.overline)
                                .padding(.horizontal)
                            
                            ScrollView(.horizontal, showsIndicators: false) {
                                HStack(spacing: 16) {
                                    ForEach(viewModel.hitlActions) { action in
                                        QuickActionHITLCard(action: action, viewModel: viewModel)
                                            .frame(width: 320)
                                    }
                                }
                                .padding(.horizontal)
                                .padding(.bottom, 8)
                            }
                        }
                    }
                    
                    // 4. Income History (Settled vs Projected)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("INCOME FLOW TIMELINE")
                            .premiumTypography(.overline)
                            .padding(.horizontal)
                        
                        IncomeFlowChart(points: viewModel.incomePoints)
                            .frame(height: 200)
                            .padding(.horizontal)
                    }
                    
                    Spacer(minLength: 40)
                }
            }
        }
    }
}

// MARK: - Subviews

struct ProbabilityCloudChart: View {
    let ranges: [ProbabilityRange]
    
    var body: some View {
        GlassCard(cornerRadius: 24) {
            Chart {
                ForEach(ranges) { range in
                    // The Cloud (Translucent shaded area)
                    AreaMark(
                        x: .value("Date", range.date),
                        yStart: .value("Min", range.lower),
                        yEnd: .value("Max", range.upper)
                    )
                    .foregroundStyle(
                        LinearGradient(
                            colors: [
                                Color.stitchNeonIndigo.opacity(0.1),
                                Color.stitchNeonCyan.opacity(0.05)
                            ],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                    )
                    .interpolationMethod(.catmullRom)
                    
                    // The Median Line
                    LineMark(
                        x: .value("Date", range.date),
                        y: .value("Price", (range.upper + range.lower) / 2)
                    )
                    .foregroundStyle(Color.stitchNeonIndigo)
                    .lineStyle(StrokeStyle(lineWidth: 2, dash: [4, 2]))
                    .interpolationMethod(.catmullRom)
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { value in
                    AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [2, 4]))
                        .foregroundStyle(.white.opacity(0.1))
                    AxisValueLabel() {
                        if let doubleValue = value.as(Double.self) {
                            Text("$\(Int(doubleValue))")
                                .premiumTypography(.caption)
                        }
                    }
                }
            }
            .chartXAxis {
                AxisMarks(values: .stride(by: .month)) { _ in
                    AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                        .foregroundStyle(.white.opacity(0.05))
                    AxisValueLabel(format: .dateTime.month(.abbreviated))
                        .premiumTypography(.caption)
                }
            }
        }
    }
}

struct QuickActionHITLCard: View {
    let action: HITLAction
    let viewModel: PassiveIncomeViewModel
    
    var body: some View {
        GlassCard(cornerRadius: 20) {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(action.ticker)
                            .premiumTypography(.title)
                            .foregroundStyle(.white)
                        Text(action.action)
                            .premiumTypography(.overline)
                            .foregroundStyle(action.action.contains("Abort") ? Color.growinRed : Color.stitchNeonGreen)
                    }
                    Spacer()
                    
                    // Dividend Badge
                    Image(systemName: "dollarsign.circle.fill")
                        .foregroundStyle(Color.stitchNeonGreen)
                        .font(.system(size: 24))
                }
                
                // Reasoning Chip
                ReasoningChip(
                    agent: action.action.contains("Abort") ? "Risk Manager" : "Technical Trader",
                    action: action.reason,
                    isActive: false
                )
                
                HStack(spacing: 12) {
                    Button(action: { viewModel.approveAction(action) }) {
                        Text("APPROVE")
                            .premiumTypography(.overline)
                            .padding(.vertical, 10)
                            .frame(maxWidth: .infinity)
                            .background(Color.stitchNeonGreen.opacity(0.15))
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.stitchNeonGreen.opacity(0.5)))
                            .foregroundStyle(Color.stitchNeonGreen)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Approve \(action.action)")
                    .accessibilityHint("Approves the pending action")
                    .accessibilityAddTraits(.isButton)
                    
                    Button(action: { viewModel.abortAction(action) }) {
                        Text("REJECT")
                            .premiumTypography(.overline)
                            .padding(.vertical, 10)
                            .frame(maxWidth: .infinity)
                            .background(Color.white.opacity(0.05))
                            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.2)))
                            .foregroundStyle(.white.opacity(0.8))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Reject \(action.action)")
                    .accessibilityHint("Rejects the pending action")
                    .accessibilityAddTraits(.isButton)
                }
            }
        }
    }
}

struct IncomeFlowChart: View {
    let points: [IncomePoint]
    
    var body: some View {
        GlassCard(cornerRadius: 24) {
            Chart {
                ForEach(points) { point in
                    if point.isSettled {
                        // Solid Emerald Green: Settled
                        BarMark(
                            x: .value("Date", point.date),
                            y: .value("Amount", point.amount)
                        )
                        .foregroundStyle(Color.stitchNeonGreen)
                        .cornerRadius(6)
                    } else {
                        // Ghost/Hollow Outline (Soft Blue): Projected
                        BarMark(
                            x: .value("Date", point.date),
                            y: .value("Amount", point.amount)
                        )
                        .foregroundStyle(Color.stitchNeonCyan.opacity(0.2))
                        .annotation(position: .top) {
                            Image(systemName: "sparkles")
                                .font(.system(size: 8))
                                .foregroundStyle(Color.stitchNeonCyan)
                        }
                    }
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { value in
                    AxisGridLine().foregroundStyle(.white.opacity(0.05))
                    AxisValueLabel() {
                        if let doubleValue = value.as(Double.self) {
                            Text("$\(Int(doubleValue))")
                                .premiumTypography(.caption)
                        }
                    }
                }
            }
            .chartXAxis {
                AxisMarks(values: .stride(by: .month)) { _ in
                    AxisValueLabel(format: .dateTime.month(.abbreviated))
                        .premiumTypography(.caption)
                }
            }
        }
    }
}

struct PassiveIncomeDashboard_Previews: PreviewProvider {
    static var previews: some View {
        PassiveIncomeDashboard()
            .preferredColorScheme(.dark)
    }
}
