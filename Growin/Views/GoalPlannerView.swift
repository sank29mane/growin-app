import SwiftUI
import Charts

struct GoalPlannerView: View {
    @Bindable var viewModel: GoalPlannerViewModel
    
    var body: some View {
        ScrollView(showsIndicators: false) {
            VStack(spacing: 32) {
                // Integrated Goal Header
                HStack(alignment: .bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("PLANNING")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundStyle(.secondary)
                        
                        Text("Goal Planner")
                            .font(.title)
                            .fontWeight(.bold)
                            .foregroundStyle(.primary)
                    }
                    
                    Spacer()
                    
                    // Goal Target Pill
                    HStack(spacing: 8) {
                        Image(systemName: "target")
                            .font(.system(size: 14))
                            .foregroundStyle(Color.growinAccent)
                        Text("TARGET")
                            .font(.caption2)
                            .fontWeight(.bold)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Color.growinAccent.opacity(0.1))
                    .clipShape(Capsule())
                }
                .padding(.horizontal)
                .padding(.top, 24)
                
                inputSection
                
                if viewModel.isLoading {
                    VStack(spacing: 24) {
                        ProgressView()
                            .scaleEffect(1.2)
                            .tint(Color.growinAccent)
                        
                        VStack(spacing: 8) {
                            Text("CALCULATING")
                                .font(.caption)
                                .fontWeight(.bold)
                                .foregroundStyle(Color.growinAccent)
                            Text("Running simulations to optimize your plan...")
                                .font(.system(size: 12))
                                .foregroundStyle(.secondary)
                        }
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
                    .transition(AnyTransition.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .padding(.bottom, 40)
        }
        .navigationTitle("")
        .alert("Strategy Deployed", isPresented: $viewModel.showExecutionConfirmation) {
            Button("Done", role: .cancel) { }
        } message: {
            Text("Investment plan '\(viewModel.plan?.implementation?.name ?? "Goal Portfolio")' has been successfully sent to Trading 212.")
        }
        .glassEffect(.thin)
    }
    
    private var inputSection: some View {
        GlassCard(cornerRadius: 32) {
            VStack(spacing: 24) {
                // Capital Area
                VStack(alignment: .leading, spacing: 16) {
                    HStack {
                        Text("INITIAL CAPITAL")
                            .font(.system(size: 10, weight: .black))
                            .tracking(1)
                            .foregroundStyle(.secondary)
                        
                        Spacer()
                        
                        HStack(spacing: 4) {
                            Text("£")
                                .font(.system(size: 16, weight: .bold))
                                .foregroundStyle(.secondary)
                            TextField("Amount", value: $viewModel.capital, format: .number)
                                .multilineTextAlignment(.trailing)
                                .textFieldStyle(.plain)
                                .font(.system(size: 24, weight: .heavy, design: .rounded))
                                .foregroundStyle(.white)
                                .frame(width: 140)
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Color.white.opacity(0.05))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    
                    Slider(value: $viewModel.capital, in: 100...100000, step: 500)
                        .tint(Color.growinPrimary)
                }
                
                Divider().background(Color.white.opacity(0.05))
                
                // Target & Duration Combined
                HStack(spacing: 24) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("TARGET RETURN")
                            .font(.system(size: 10, weight: .black))
                            .tracking(1)
                            .foregroundStyle(.secondary)
                        
                        HStack {
                            Text("\(Int(viewModel.targetReturn))%")
                                .font(.system(size: 20, weight: .bold, design: .rounded))
                            Spacer()
                            Stepper("", value: $viewModel.targetReturn, in: 2...50)
                                .labelsHidden()
                                .scaleEffect(0.8)
                        }
                        .padding(12)
                        .background(Color.white.opacity(0.05))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    .frame(maxWidth: .infinity)
                    
                    VStack(alignment: .leading, spacing: 12) {
                        Text("DURATION")
                            .font(.system(size: 10, weight: .black))
                            .tracking(1)
                            .foregroundStyle(.secondary)
                        
                        HStack {
                            Text("\(Int(viewModel.durationYears)) Yrs")
                                .font(.system(size: 20, weight: .bold, design: .rounded))
                            Spacer()
                            Stepper("", value: $viewModel.durationYears, in: 1...30)
                                .labelsHidden()
                                .scaleEffect(0.8)
                        }
                        .padding(12)
                        .background(Color.white.opacity(0.05))
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                    }
                    .frame(maxWidth: .infinity)
                }
                
                Divider().background(Color.white.opacity(0.05))
                
                // Risk Selector
                VStack(alignment: .leading, spacing: 16) {
                    Text("RISK APPETITE")
                        .font(.system(size: 10, weight: .black))
                        .tracking(1)
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
                }
                
                Button(action: {
                    Task { await viewModel.generatePlan() }
                }) {
                    HStack(spacing: 10) {
                        Text("Generate Plan")
                        Image(systemName: "sparkles")
                    }
                    .font(.system(size: 14, weight: .black))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
                    .background(
                        LinearGradient(colors: [Color.growinPrimary, Color.growinAccent], startPoint: .leading, endPoint: .trailing)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .foregroundStyle(.white)
                    .shadow(color: Color.growinPrimary.opacity(0.3), radius: 15, x: 0, y: 8)
                }
                .buttonStyle(.plain)
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
                    VStack(spacing: 12) {
                        let score = Double(truncating: plan.probabilityOfSuccess as NSNumber)
                        FeasibilityGauge(score: score)
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
            VStack(spacing: 20) {
                HStack {
                    Image(systemName: "info.circle.fill")
                        .foregroundStyle(Color.growinAccent)
                    Text("This plan is optimized for Trading 212 Pie execution.")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }
                
                SlideToConfirm(title: "SLIDE TO EXECUTE STRATEGY") {
                    Task { await viewModel.executePlan() }
                }
                .padding(.top, 10)
            }
            .padding(.top, 10)
        }
        .padding(.horizontal)
        .transition(.asymmetric(
            insertion: AnyTransition.move(edge: .bottom).combined(with: .opacity),
            removal: .opacity
        ))
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
                .foregroundStyle(by: .value("Series", "Projected"))
                .lineStyle(StrokeStyle(lineWidth: 3))
                .interpolationMethod(.catmullRom)
                
                AreaMark(
                    x: .value("Year", point.year),
                    y: .value("Expected", val)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [Color.growinPrimary.opacity(0.3), Color.growinPrimary.opacity(0.0)],
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
                .foregroundStyle(by: .value("Series", "Target"))
                .lineStyle(StrokeStyle(lineWidth: 2, dash: [5, 5]))
                .interpolationMethod(.catmullRom)
            }
        }
        .chartForegroundStyleScale([
            "Projected": Color.growinPrimary,
            "Target": Color.white.opacity(0.3)
        ])
        .chartXAxis {
            AxisMarks(values: .automatic) { value in
                AxisGridLine(stroke: StrokeStyle(dash: [2, 4])).foregroundStyle(.white.opacity(0.1))
                AxisValueLabel() {
                    if let year = value.as(Double.self) {
                        Text("Y\(Int(year))")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine(stroke: StrokeStyle(dash: [2, 4])).foregroundStyle(.white.opacity(0.1))
                AxisValueLabel() {
                    if let val = value.as(Double.self) {
                        Text("£\(Int(val/1000))k")
                            .font(.system(size: 9, weight: .bold))
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
                    .font(.system(size: 9, weight: .black))
                    .tracking(0.5)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(isSelected ? Color.growinPrimary.opacity(0.2) : Color.white.opacity(0.05))
            .clipShape(.rect(cornerRadius: 16))
            .overlay(
                RoundedRectangle(cornerRadius: 16)
                    .stroke(isSelected ? Color.growinPrimary : .white.opacity(0.1), lineWidth: 1.5)
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
                .stroke(Color.white.opacity(0.05), lineWidth: 10)
            
            Circle()
                .trim(from: 0, to: score)
                .stroke(
                    LinearGradient(colors: [Color.growinOrange, Color.growinGreen], startPoint: .top, endPoint: .bottom),
                    style: StrokeStyle(lineWidth: 10, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .shadow(color: Color.growinGreen.opacity(0.3), radius: 5)
            
            VStack(spacing: 0) {
                Text("\(Int(score * 100))%")
                    .font(.system(size: 20, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                Text("CHANCE")
                    .font(.caption2)
                    .fontWeight(.bold)
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
                        let weight = Double(truncating: inst.weight as NSNumber)
                        Text("\(Int(weight * 100))%")
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
