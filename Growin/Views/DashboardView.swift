import SwiftUI
import Charts

struct DashboardView: View {
    @Bindable var viewModel: DashboardViewModel
    @State private var selectedPosition: Position?
    
    // Define GridItems for a flexible Bento-like layout
    private var bentoGridItems: [GridItem] {
        [GridItem(.flexible()), GridItem(.flexible())]
    }

    var body: some View {
        ZStack {
            MeshBackground()
            
            ScrollView {
                VStack(spacing: 24) {
                    // Error Banner
                    if let error = viewModel.errorMessage {
                        ErrorCard(message: error) {
                            Task { await viewModel.fetchPortfolioData() }
                        }
                        .transition(.move(edge: .top).combined(with: .opacity))
                    }

                    // Header
                    AppHeader(
                        title: "Market Intelligence",
                        subtitle: "Real-time portfolio pulse",
                        icon: "chart.bar.fill"
                    )
                    .padding(.horizontal)
                    
                    // SOTA Intelligence Preview (New)
                    VStack(alignment: .leading, spacing: 12) {
                        Text("AGENT INTELLIGENCE")
                            .premiumTypography(.overline)
                            .padding(.horizontal)
                        
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 12) {
                                ReasoningChip(agent: "Portfolio Analyst", action: "Scanning ISA Exposure", isActive: true)
                                ReasoningChip(agent: "Risk Manager", action: "Stable", isActive: false)
                                ReasoningChip(agent: "Technical Trader", action: "Awaiting Confirmation", isActive: false)
                                
                                ConfidenceIndicator(score: 0.94)
                                ConfidenceIndicator(score: 0.72)
                                ConfidenceIndicator(score: 0.45)
                            }
                            .padding(.horizontal)
                        }
                    }
                    
                    // Bento Grid for Key Metrics and Charts
                    LazyVGrid(columns: bentoGridItems, spacing: 16) {
                        // Portfolio Summary Metrics (using FinancialMetricView)
                        Group {
                            if let summary = viewModel.portfolioData?.summary {
                                let totalCap = (summary.currentValue ?? Decimal(0)) + (summary.cashBalance?.free ?? Decimal(0))
                                FinancialMetricView(
                                    title: "TOTAL CAPITAL",
                                    value: "£\(totalCap.formatted(.number.precision(.fractionLength(2))))",
                                    change: nil, changePositive: nil
                                )
                                
                                let equity = summary.currentValue ?? Decimal(0)
                                FinancialMetricView(
                                    title: "EQUITY",
                                    value: "£\(equity.formatted(.number.precision(.fractionLength(2))))",
                                    change: nil, changePositive: nil
                                )
                                
                                let pnl = summary.totalPnl ?? Decimal(0)
                                let pnlPercent = summary.totalPnlPercent ?? 0.0
                                FinancialMetricView(
                                    title: "NET PROFIT",
                                    value: "£\(pnl.formatted(.number.precision(.fractionLength(2))))",
                                    change: "\(pnl >= 0 ? "+" : "")\(pnlPercent.formatted(.number.precision(.fractionLength(2))))%",
                                    changePositive: pnl >= 0
                                )
                                
                                let cash = summary.cashBalance?.free ?? Decimal(0)
                                FinancialMetricView(
                                    title: "CASH BALANCE",
                                    value: "£\(cash.formatted(.number.precision(.fractionLength(2))))",
                                    change: nil, changePositive: nil
                                )
                            } else {
                                ForEach(0..<4) { _ in
                                    GlassCard {
                                        VStack(alignment: .leading) {
                                            Text("Loading...")
                                                .premiumTypography(.caption)
                                            ProgressView()
                                        }
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding()
                                    }
                                    .frame(minHeight: 100)
                                }
                            }
                        }
                        
                        // Allocation Pie Chart (within a GlassCard to fit Bento grid)
                        GlassCard {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("ALLOCATION VECTORS")
                                    .premiumTypography(.overline)
                                
                                if !viewModel.allocationData.isEmpty {
                                    Chart(viewModel.allocationData) { item in
                                        if item.doubleValue > 0 {
                                            SectorMark(
                                                angle: .value("Value", item.doubleValue),
                                                innerRadius: .ratio(0.618),
                                                angularInset: 1.5
                                            )
                                            .cornerRadius(5)
                                            .foregroundStyle(by: .value("Name", item.label))
                                        }
                                    }
                                    .frame(height: 150)
                                    .chartLegend(.hidden)
                                    .chartForegroundStyleScale([
                                        "Others": Color.textSecondary.opacity(0.3)
                                    ])
                                } else {
                                    ContentUnavailableView("No Data", systemImage: "chart.pie")
                                        .scaleEffect(0.5)
                                }
                            }
                            .padding() // Padding inside GlassCard for content
                        }
                        .frame(minHeight: 200) // Adjust size for Bento
                        
                        // Performance Delta (within a GlassCard to fit Bento grid)
                        GlassCard {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("PERFORMANCE DELTA")
                                    .premiumTypography(.overline)
                                
                                if let pnl = viewModel.portfolioData?.summary?.totalPnl,
                                   let pnlPercent = viewModel.portfolioData?.summary?.totalPnlPercent {
                                    
                                    Spacer()
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text("£\(pnl.formatted(.number.precision(.fractionLength(2))))")
                                            .premiumTypography(.heading)
                                            .foregroundStyle(pnl >= 0 ? Color.stitchNeonGreen : .growinRed)
                                        
                                        HStack(spacing: 4) {
                                            Image(systemName: pnl >= 0 ? "arrow.up.right" : "arrow.down.right")
                                            Text("\(pnl >= 0 ? "+" : "")\(pnlPercent.formatted(.number.precision(.fractionLength(2))))%")
                                        }
                                        .premiumTypography(.title)
                                        .foregroundStyle(pnl >= 0 ? Color.stitchNeonGreen : .growinRed)
                                    }
                                    Spacer()
                                } else {
                                    ContentUnavailableView("No Data", systemImage: "chart.line.uptrend.xyaxis")
                                        .scaleEffect(0.5)
                                }
                            }
                            .padding() // Padding inside GlassCard for content
                        }
                        .frame(minHeight: 200) // Adjust size for Bento
                    }
                    .padding(.horizontal)
                    
                    // Account Sections (still in HStack for now, will integrate into Bento later)
                    HStack(spacing: 16) {
                        // INVEST Account Section
                        AccountSectionView(
                            title: "INVESTMENT PORTFOLIO",
                            accountData: viewModel.investData,
                            isLoading: viewModel.isLoading,
                            onPositionTap: { position in
                                selectedPosition = position
                            }
                        )

                        // ISA Account Section
                        AccountSectionView(
                            title: "ISA STRATEGIC ASSETS",
                            accountData: viewModel.isaData,
                            isLoading: viewModel.isLoading,
                            onPositionTap: { position in
                                selectedPosition = position
                            }
                        )
                    }
                    .padding(.horizontal)
                }
                .padding(.vertical)
            }
            .refreshable {
                await viewModel.fetchPortfolioData()
            }
        }
        .sheet(item: $selectedPosition) { position in
            if let ticker = position.ticker {
                NavigationStack {
                    StockChartView(viewModel: StockChartViewModel(symbol: ticker))
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Close") {
                                    selectedPosition = nil
                                }
                            }
                        }
                }
                .frame(minWidth: 800, minHeight: 600)
            }
        }
        .navigationTitle("Market Intelligence")
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button(action: {
                    Task { await viewModel.fetchPortfolioData() }
                }) {
                    Image(systemName: "arrow.clockwise")
                }
                .accessibilityLabel("Refresh Portfolio")
                .disabled(viewModel.isLoading)
            }
        }
    }
}

struct AccountSectionView: View {
    let title: String
    let accountData: AccountData?
    let isLoading: Bool
    let onPositionTap: (Position) -> Void

    var body: some View {
        GlassCard {
            VStack(alignment: .leading, spacing: 16) {
                Text(title)
                    .premiumTypography(.overline)
                    .padding(.horizontal)

                if let data = accountData {
                    // Mini metrics for this account, now using FinancialMetricView
                    VStack(spacing: 8) {
                        HStack(spacing: 8) {
                            let val = data.summary.currentValue ?? Decimal(0)
                            FinancialMetricView(
                                title: "EQUITY",
                                value: "£\(val.formatted(.number.precision(.fractionLength(0))))",
                                change: nil, changePositive: nil
                            )
                            
                            let pnl = data.summary.totalPnl ?? Decimal(0)
                            FinancialMetricView(
                                title: "DELTA",
                                value: "\(pnl >= 0 ? "+" : "")£\(pnl.formatted(.number.precision(.fractionLength(0))))",
                                change: nil, changePositive: nil
                            )
                        }
                        HStack(spacing: 8) {
                            FinancialMetricView(
                                title: "ALPHA",
                                value: {
                                    if let pnl = data.summary.totalPnl, let invested = data.summary.totalInvested, invested > 0 {
                                        let percent = (pnl / invested) * 100
                                        return "\(percent.formatted(.number.precision(.fractionLength(1))))%"
                                    } else {
                                        return "N/A"
                                    }
                                }(),
                                change: nil, changePositive: nil
                            )
                            
                            let cash = data.summary.cashBalance?.free ?? Decimal(0)
                            FinancialMetricView(
                                title: "LIQUIDITY",
                                value: "£\(cash.formatted(.number.precision(.fractionLength(0))))",
                                change: nil, changePositive: nil
                            )
                        }
                    }
                    .padding(.horizontal)

                    // Allocation chart for this account
                    if !data.allocationData.isEmpty {
                        GlassCard {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("CONCENTRATION")
                                    .premiumTypography(.overline)

                                Chart(data.allocationData) { item in
                                    if item.doubleValue > 0 {
                                        SectorMark(
                                            angle: .value("Value", item.doubleValue),
                                            innerRadius: .ratio(0.618),
                                            angularInset: 1.5
                                        )
                                        .cornerRadius(5)
                                        .foregroundStyle(by: .value("Name", item.label))
                                    }
                                }
                                .frame(height: 120)
                                .chartLegend(position: .bottom)
                            }
                        }
                    }

                    // Positions for this account
                    VStack(alignment: .leading, spacing: 8) {
                        Text("STRATEGIC HOLDINGS")
                            .premiumTypography(.overline)
                            .padding(.horizontal)

                        ScrollView {
                            LazyVStack(spacing: 8) {
                                ForEach(data.positions) { position in
                                    PositionDeepCard(position: position)
                                        .onTapGesture {
                                            onPositionTap(position)
                                        }
                                }
                            }
                            .padding(.horizontal)
                        }
                        .frame(height: 300) // Fixed height for scrolling
                    }
                } else if isLoading {
                    ProgressView()
                        .padding()
                } else {
                    ContentUnavailableView("No Data", systemImage: "tray")
                        .scaleEffect(0.5)
                        .padding()
                }
            }
        }
    }
}
