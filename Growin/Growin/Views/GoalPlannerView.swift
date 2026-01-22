import SwiftUI
import Charts

struct GoalPlannerView: View {
    @ObservedObject var viewModel: GoalPlannerViewModel
    
    var body: some View {
        ZStack {
            GradientBackground()
            
            ScrollView {
                VStack(spacing: 30) {
                    AppHeader(
                        title: "Goal Planner",
                        subtitle: "AI-optimized portfolios for your financial North Star.",
                        icon: "target"
                    )
                    
                    inputSection
                    
                    if viewModel.isLoading {
                        VStack(spacing: 16) {
                            ProgressView()
                                .scaleEffect(1.5)
                                .tint(.blue)
                            Text("Running Monte Carlo simulations...")
                                .font(.system(size: 14, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                        .padding(.top, 40)
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
                .padding(24)
            }
        }
        .navigationTitle("Goal Planner")
        .alert("Strategy Deployed", isPresented: $viewModel.showExecutionConfirmation) {
            Button("Done", role: .cancel) { }
        } message: {
            Text("Investment strategy '\(viewModel.plan?.implementation?.name ?? "Goal Portfolio")' has been successfully sent to Trading 212.")
        }
    }
    
    private var inputSection: some View {
        GlassCard(cornerRadius: 24) {
            VStack(spacing: 24) {
                // Capital Area
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Label("INITIAL CAPITAL", systemImage: "sterlingsign.circle.fill")
                            .font(.system(size: 10, weight: .black))
                            .foregroundStyle(.secondary)
                        Spacer()
                        
                        TextField("Amount", value: $viewModel.capital, format: .number)
                            .multilineTextAlignment(.trailing)
                            .textFieldStyle(.plain)
                            .font(.system(size: 20, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 120)
                            .padding(.horizontal, 8)
                            .background(Color.white.opacity(0.1))
                            .cornerRadius(8)
                    }
                    
                    Slider(value: $viewModel.capital, in: 100...100000, step: 500)
                        .tint(.blue)
                }
                
                Divider().background(Color.white.opacity(0.1))
                
                // Target & Duration Combined
                HStack(spacing: 20) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("TARGET RETURN (%)")
                            .font(.system(size: 10, weight: .black))
                            .foregroundStyle(.secondary)
                        
                        Stepper("\(Int(viewModel.targetReturn))%", value: $viewModel.targetReturn, in: 2...50)
                            .font(.system(size: 16, weight: .bold))
                    }
                    .frame(maxWidth: .infinity)
                    
                    VStack(alignment: .leading, spacing: 12) {
                        Text("DURATION (YEARS)")
                            .font(.system(size: 10, weight: .black))
                            .foregroundStyle(.secondary)
                        
                        HStack {
                            TextField("Yrs", value: $viewModel.durationYears, format: .number)
                                .textFieldStyle(.plain)
                                .font(.system(size: 16, weight: .bold))
                                .frame(width: 30)
                                .multilineTextAlignment(.trailing)
                            
                            Stepper("", value: $viewModel.durationYears, in: 1...30)
                                .labelsHidden()
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                
                Divider().background(Color.white.opacity(0.1))
                
                // Risk Selector
                VStack(alignment: .leading, spacing: 12) {
                    Text("RISK APPETITE")
                        .font(.system(size: 10, weight: .black))
                        .foregroundStyle(.secondary)
                    
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
                    
                    Text(viewModel.riskDescription(for: viewModel.selectedRisk))
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.blue)
                        .padding(.top, 4)
                }
                
                Button(action: {
                    Task { await viewModel.generatePlan() }
                }) {
                    HStack {
                        Text("GENERATE AI PLAN")
                            .font(.system(size: 14, weight: .black))
                        Image(systemName: "sparkles")
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(
                        LinearGradient(colors: [.blue, .purple], startPoint: .leading, endPoint: .trailing)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .foregroundStyle(.white)
                    .shadow(color: .blue.opacity(0.3), radius: 10, x: 0, y: 5)
                }
                .buttonStyle(.plain)
            }
        }
    }
    
    private func planResultsSection(plan: GoalPlan) -> some View {
        VStack(spacing: 24) {
            // Summary Row
            HStack(spacing: 16) {
                GlassCard {
                    VStack(spacing: 12) {
                        FeasibilityGauge(score: plan.probabilityOfSuccess)
                        Text("Success Probability")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                }
                
                GlassCard {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("GROWTH PROJECTION")
                            .font(.system(size: 10, weight: .black))
                            .foregroundStyle(.secondary)
                        
                        GrowthChart(points: plan.simulatedGrowthPath ?? [])
                            .frame(height: 180)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            
            // Asset Allocation
            GlassCard(cornerRadius: 20) {
                VStack(alignment: .leading, spacing: 16) {
                    Text("OPTIMAL ASSET MIX")
                        .font(.system(size: 12, weight: .black))
                        .foregroundStyle(.secondary)
                    
                    AssetAllocationList(instruments: plan.suggestedInstruments)
                }
            }
            
            // Strategy Execution Call to Action
            VStack(spacing: 12) {
                HStack {
                    Image(systemName: "info.circle.fill")
                        .foregroundStyle(.blue)
                    Text("This plan is optimized for Trading 212 Pie execution.")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
                
                Button(action: {
                    Task { await viewModel.executePlan() }
                }) {
                    HStack {
                        Image(systemName: "tray.and.arrow.down.fill")
                        Text("DEPLOY TO TRADING 212")
                            .font(.system(size: 14, weight: .black))
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
                    .background(Color.white)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                    .foregroundStyle(.black)
                }
                .buttonStyle(.plain)
            }
            .padding(.top, 10)
        }
        .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
    }
}

// MARK: - Subcomponents

struct GrowthChart: View {
    let points: [GrowthPoint]
    
    var body: some View {
        Chart {
            ForEach(points) { point in
                LineMark(
                    x: .value("Year", point.year),
                    y: .value("Expected", point.value),
                    series: .value("Series", "Projected")
                )
                .foregroundStyle(by: .value("Series", "Projected"))
                .interpolationMethod(.catmullRom)
                
                AreaMark(
                    x: .value("Year", point.year),
                    y: .value("Expected", point.value)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [.blue.opacity(0.3), .blue.opacity(0.0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
                
                LineMark(
                    x: .value("Year", point.year),
                    y: .value("Target", point.target),
                    series: .value("Series", "Target")
                )
                .foregroundStyle(by: .value("Series", "Target"))
                .lineStyle(StrokeStyle(lineWidth: 2, dash: [5, 5]))
                .interpolationMethod(.catmullRom)
            }
        }
        .chartForegroundStyleScale([
            "Projected": Color.blue,
            "Target": Color.white.opacity(0.5)
        ])
        .chartXAxis {
            AxisMarks(values: .automatic) { value in
                AxisGridLine().foregroundStyle(.white.opacity(0.05))
                AxisValueLabel() {
                    if let year = value.as(Double.self) {
                        Text("Y\(Int(year))")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine().foregroundStyle(.white.opacity(0.05))
                AxisValueLabel() {
                    if let val = value.as(Double.self) {
                        Text("£\(Int(val/1000))k")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
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
                    .font(.system(size: 20))
                Text(title.replacingOccurrences(of: "_", with: " "))
                    .font(.system(size: 9, weight: .bold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .background(isSelected ? Color.blue.opacity(0.3) : Color.white.opacity(0.05))
            .cornerRadius(12)
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(isSelected ? Color.blue : Color.white.opacity(0.1), lineWidth: 1)
            )
            .foregroundStyle(isSelected ? .white : .secondary)
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
                    LinearGradient(colors: [.orange, .green], startPoint: .top, endPoint: .bottom),
                    style: StrokeStyle(lineWidth: 8, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .shadow(color: .green.opacity(0.3), radius: 4)
            
            VStack(spacing: 0) {
                Text("\(Int(score * 100))%")
                    .font(.system(size: 20, weight: .black))
                    .foregroundStyle(.white)
                Text("Success")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 80, height: 80)
    }
}

struct AssetAllocationList: View {
    let instruments: [SuggestedInstrument]
    
    var body: some View {
        VStack(spacing: 12) {
            ForEach(instruments) { inst in
                HStack {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.blue.opacity(0.1))
                            .frame(width: 36, height: 36)
                        Text(inst.ticker.prefix(2))
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(.blue)
                    }
                    
                    VStack(alignment: .leading, spacing: 2) {
                        Text(inst.ticker)
                            .font(.system(size: 14, weight: .bold))
                            .foregroundStyle(.white)
                        Text(inst.name)
                            .font(.system(size: 10))
                            .lineLimit(1)
                            .foregroundStyle(.secondary)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 2) {
                        Text("\(Int(inst.weight * 100))%")
                            .font(.system(size: 14, weight: .black))
                            .foregroundStyle(.white)
                        Text(inst.category)
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.blue.opacity(0.8))
                    }
                }
                
                if inst.id != instruments.last?.id {
                    Divider().background(Color.white.opacity(0.05))
                }
            }
        }
    }
}
