import SwiftUI
import Charts

struct GoalPlannerView: View {
    @Bindable var viewModel: GoalPlannerViewModel
    
    var body: some View {
        GeometryReader { geo in
            let isWide = geo.size.width > 900
            
            HStack(spacing: 0) {
                if isWide {
                    // AI Strategy Hub Sidebar (Wide Layout)
                    VStack(spacing: 0) {
                        ScrollView {
                            inputSection
                                .padding(.vertical, 40)
                        }
                    }
                    .frame(width: 420)
                    .background(
                        Rectangle()
                            .fill(.ultraThinMaterial)
                            .opacity(0.5)
                    )
                    .overlay(alignment: .trailing) {
                        Divider().background(Color.white.opacity(0.1))
                    }
                }
                
                ScrollView(showsIndicators: false) {
                    VStack(spacing: 32) {
                        AppHeader(
                            title: "Goal Planner",
                            subtitle: "Strategic Wealth Projection",
                            icon: "target"
                        )
                        .padding(.horizontal)
                        .padding(.top, 24)
                        
                        if !isWide {
                            inputSection
                        }
                        
                        if viewModel.isLoading {
                            VStack(spacing: 24) {
                                ProgressView()
                                    .scaleEffect(1.5)
                                    .tint(Color.stitchNeonIndigo)
                                
                                VStack(spacing: 8) {
                                    Text("COMPUTING")
                                        .premiumTypography(.overline)
                                        .foregroundStyle(Color.stitchNeonIndigo)
                                    Text("Optimizing risk-adjusted returns...")
                                        .premiumTypography(.caption)
                                }
                            }
                            .padding(.top, 40)
                            .frame(maxWidth: .infinity)
                        }
                        
                        if let plan = viewModel.plan {
                            planResultsSection(plan: plan)
                        }
                        
                        if let error = viewModel.errorMsg {
                            ErrorCard(message: error) {
                                Task { await viewModel.generatePlan() }
                            }
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                        }
                    }
                    .padding(.bottom, 60)
                }
                .frame(maxWidth: .infinity)
            }
        }
        .navigationTitle("")
        .alert("Strategy Deployed", isPresented: $viewModel.showExecutionConfirmation) {
            Button("Done", role: .cancel) { }
        } message: {
            Text("Investment plan '\(viewModel.plan?.implementation?.name ?? "Goal Portfolio")' has been successfully sent to Trading 212.")
        }
    }
    
    private var inputSection: some View {
        GlassCard(cornerRadius: 32) {
            VStack(spacing: 32) {
                // Capital Area - Scenario Simulator
                VStack(alignment: .leading, spacing: 16) {
                    HStack {
                        Text("SCENARIO SIMULATOR")
                            .premiumTypography(.overline)
                        
                        Spacer()
                        
                        HStack(spacing: 4) {
                            Text("£")
                                .font(.system(size: 20, weight: .bold, design: .rounded))
                                .foregroundStyle(Color.textSecondary)
                            TextField("Amount", value: $viewModel.capital, format: .number)
                                .multilineTextAlignment(.trailing)
                                .textFieldStyle(.plain)
                                .font(.system(size: 28, weight: .heavy, design: .rounded))
                                .foregroundStyle(.white)
                                .frame(width: 160)
                        }
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .background(Color.white.opacity(0.03))
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .overlay(
                            RoundedRectangle(cornerRadius: 16)
                                .stroke(Color.white.opacity(0.1), lineWidth: 1)
                        )
                    }
                    
                    CustomSlider(value: $viewModel.capital, range: 100...100000, step: 500)
                }
                
                Divider().background(Color.white.opacity(0.05))
                
                // Target & Duration Combined
                HStack(spacing: 20) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("TARGET ROI")
                            .premiumTypography(.overline)
                        
                        HStack {
                            Text("\(Int(viewModel.targetReturn))%")
                                .font(.system(size: 24, weight: .bold, design: .rounded))
                            Spacer()
                            Stepper("", value: $viewModel.targetReturn, in: 2...50)
                                .labelsHidden()
                                .scaleEffect(0.9)
                        }
                        .padding(16)
                        .background(Color.white.opacity(0.03))
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                    }
                    
                    VStack(alignment: .leading, spacing: 12) {
                        Text("HORIZON")
                            .premiumTypography(.overline)
                        
                        HStack {
                            Text("\(Int(viewModel.durationYears))Y")
                                .font(.system(size: 24, weight: .bold, design: .rounded))
                            Spacer()
                            Stepper("", value: $viewModel.durationYears, in: 1...30)
                                .labelsHidden()
                                .scaleEffect(0.9)
                        }
                        .padding(16)
                        .background(Color.white.opacity(0.03))
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                    }
                }
                
                Divider().background(Color.white.opacity(0.05))
                
                // Risk Selector
                VStack(alignment: .leading, spacing: 16) {
                    Text("RISK PROFILE")
                        .premiumTypography(.overline)
                    
                    HStack(spacing: 12) {
                        ForEach(viewModel.riskOptions, id: \.self) { risk in
                            RiskButton(
                                title: risk,
                                isSelected: viewModel.selectedRisk == risk,
                                icon: viewModel.riskIcon(for: risk)
                            ) {
                                withAnimation(.spring(response: 0.3)) {
                                    viewModel.selectedRisk = risk
                                }
                            }
                        }
                    }
                }
                
                PremiumButton(title: "Generate Strategy", icon: "sparkles", color: Color.stitchNeonIndigo) {
                    Task { await viewModel.generatePlan() }
                }
                .frame(maxWidth: .infinity)
            }
            .padding(24)
        }
        .padding(.horizontal)
    }
    
    private func planResultsSection(plan: GoalPlan) -> some View {
        VStack(spacing: 24) {
            // Summary Row
            HStack(spacing: 16) {
                GlassCard {
                    VStack(spacing: 16) {
                        let score = Double(truncating: plan.probabilityOfSuccess as NSNumber)
                        FeasibilityGauge(score: score)
                        
                        Text("FEASIBILITY")
                            .premiumTypography(.overline)
                    }
                    .frame(maxWidth: .infinity)
                }
                .frame(width: 140)
                
                GlassCard {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("PROJECTION MATRIX")
                            .premiumTypography(.overline)
                        
                        GrowthChart(points: plan.simulatedGrowthPath ?? [])
                            .frame(height: 140)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            
            // Asset Allocation
            GlassCard(cornerRadius: 24) {
                VStack(alignment: .leading, spacing: 20) {
                    HStack {
                        Text("ASSET COMPOSITION")
                            .premiumTypography(.overline)
                        Spacer()
                        Text("\(plan.suggestedInstruments.count) ASSETS")
                            .premiumTypography(.caption)
                            .foregroundStyle(Color.stitchNeonCyan)
                    }
                    
                    AssetAllocationList(instruments: plan.suggestedInstruments)
                }
            }
            
            // Strategy Execution
            VStack(spacing: 24) {
                HStack(spacing: 12) {
                    Image(systemName: "cpu")
                        .font(.system(size: 20))
                        .foregroundStyle(Color.stitchNeonIndigo)
                    Text("Automated rebalancing enabled for this strategy.")
                        .premiumTypography(.caption)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(Color.stitchNeonIndigo.opacity(0.1))
                .clipShape(Capsule())
                
                SlideToConfirm(title: "SLIDE TO DEPLOY STRATEGY") {
                    Task { await viewModel.executePlan() }
                }
            }
            .padding(.top, 12)
        }
        .padding(.horizontal)
        .transition(.asymmetric(
            insertion: .move(edge: .bottom).combined(with: .opacity),
            removal: .opacity
        ))
    }
}

// MARK: - Enhanced Subcomponents

struct CustomSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                // Track
                Rectangle()
                    .fill(Color.white.opacity(0.05))
                    .frame(height: 8)
                    .clipShape(Capsule())
                
                // Progress
                Rectangle()
                    .fill(
                        LinearGradient(
                            colors: [Color.stitchNeonIndigo, Color.stitchNeonCyan],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                    )
                    .frame(width: max(0, CGFloat((value - range.lowerBound) / (range.upperBound - range.lowerBound)) * geo.size.width), height: 8)
                    .clipShape(Capsule())
                    .shadow(color: Color.stitchNeonIndigo.opacity(0.3), radius: 10, x: 0, y: 0)
                
                // Handle
                Circle()
                    .fill(.white)
                    .frame(width: 24, height: 24)
                    .shadow(color: .black.opacity(0.5), radius: 5)
                    .offset(x: max(0, CGFloat((value - range.lowerBound) / (range.upperBound - range.lowerBound)) * geo.size.width) - 12)
                    .gesture(
                        DragGesture()
                            .onChanged { gesture in
                                let newValue = range.lowerBound + Double(gesture.location.x / geo.size.width) * (range.upperBound - range.lowerBound)
                                value = Swift.min(Swift.max(range.lowerBound, (newValue / step).rounded() * step), range.upperBound)
                            }
                    )
            }
        }
        .frame(height: 24)
    }
}

// MARK: - Subcomponents

struct GrowthChart: View {
    let points: [GrowthPoint]
    
    var body: some View {
        Chart {
            ForEach(points) { point in
                let val = Double(truncating: point.value as NSNumber)
                let tgt = Double(truncating: point.target as NSNumber)
                
                LineMark(
                    x: .value("Year", point.year),
                    y: .value("Expected", val),
                    series: .value("Series", "Projected")
                )
                .foregroundStyle(Color.stitchNeonIndigo)
                .lineStyle(StrokeStyle(lineWidth: 3, lineCap: .round))
                .interpolationMethod(.catmullRom)
                
                AreaMark(
                    x: .value("Year", point.year),
                    y: .value("Expected", val)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [Color.stitchNeonIndigo.opacity(0.2), Color.stitchNeonIndigo.opacity(0.0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
                
                LineMark(
                    x: .value("Year", point.year),
                    y: .value("Target", tgt),
                    series: .value("Series", "Target")
                )
                .foregroundStyle(Color.white.opacity(0.15))
                .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 4]))
                .interpolationMethod(.catmullRom)
            }
        }
        .chartXAxis {
            AxisMarks(values: .automatic) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(.white.opacity(0.05))
                AxisValueLabel() {
                    if let year = value.as(Double.self) {
                        Text("Y\(Int(year))")
                            .premiumTypography(.overline)
                            .font(.system(size: 8))
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(.white.opacity(0.05))
                AxisValueLabel() {
                    if let val = value.as(Double.self) {
                        Text("£\(Int(val/1000))k")
                            .premiumTypography(.overline)
                            .font(.system(size: 8))
                    }
                }
            }
        }
    }
}

struct RiskButton: View {
    let title: String
    let isSelected: Bool
    let icon: String
    let action: () -> Void
    
    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 22))
                    .foregroundStyle(isSelected ? Color.stitchNeonIndigo : Color.textSecondary)
                
                Text(title.replacingOccurrences(of: "_", with: " "))
                    .premiumTypography(.overline)
                    .font(.system(size: 8))
                    .foregroundStyle(isSelected ? .white : Color.textSecondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .background(isSelected ? Color.stitchNeonIndigo.opacity(0.15) : Color.white.opacity(0.03))
            .clipShape(RoundedRectangle(cornerRadius: 20))
            .overlay(
                RoundedRectangle(cornerRadius: 20)
                    .stroke(isSelected ? Color.stitchNeonIndigo : Color.white.opacity(0.1), lineWidth: 1.5)
            )
        }
        .buttonStyle(.plain)
    }
}

struct FeasibilityGauge: View {
    let score: Double // 0.0 to 1.0
    
    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.white.opacity(0.05), lineWidth: 8)
            
            Circle()
                .trim(from: 0, to: score)
                .stroke(
                    LinearGradient(
                        colors: [Color.stitchNeonYellow, Color.stitchNeonGreen],
                        startPoint: .top,
                        endPoint: .bottom
                    ),
                    style: StrokeStyle(lineWidth: 10, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .shadow(color: Color.stitchNeonGreen.opacity(0.3), radius: 8)
            
            VStack(spacing: -2) {
                Text("\(Int(score * 100))%")
                    .font(.system(size: 24, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                Text("SCORE")
                    .premiumTypography(.overline)
                    .font(.system(size: 8))
            }
        }
        .frame(width: 90, height: 90)
    }
}

struct AssetAllocationList: View {
    let instruments: [SuggestedInstrument]
    
    var body: some View {
        VStack(spacing: 16) {
            ForEach(instruments) { inst in
                HStack(spacing: 16) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 12)
                            .fill(Color.stitchNeonIndigo.opacity(0.1))
                            .frame(width: 44, height: 44)
                        Text(inst.ticker.prefix(2))
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(Color.stitchNeonIndigo)
                    }
                    
                    VStack(alignment: .leading, spacing: 4) {
                        Text(inst.ticker)
                            .premiumTypography(.title)
                            .font(.system(size: 16))
                        Text(inst.name)
                            .premiumTypography(.caption)
                            .lineLimit(1)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 4) {
                        let weight = Double(truncating: inst.weight as NSNumber)
                        Text("\(Int(weight * 100))%")
                            .premiumTypography(.title)
                            .foregroundStyle(Color.stitchNeonGreen)
                        Text(inst.category)
                            .premiumTypography(.overline)
                            .font(.system(size: 8))
                    }
                }
                
                if inst.id != instruments.last?.id {
                    Divider().background(Color.white.opacity(0.05))
                }
            }
        }
    }
}
