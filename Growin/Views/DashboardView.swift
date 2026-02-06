import SwiftUI
import Charts

struct DashboardView: View {
    @Bindable var viewModel: DashboardViewModel
    @State private var selectedPosition: Position?
    
    var body: some View {
        ZStack {
            GradientBackground()
            
            ScrollView {
                VStack(spacing: 24) {
                    // Header
                    AppHeader(
                        title: "Market Intelligence",
                        subtitle: "Real-time portfolio pulse",
                        icon: "chart.bar.fill"
                    )
                    .padding(.horizontal)
                    
                    // Portfolio Summary Grid
                    MetricGrid(summary: viewModel.portfolioData?.summary)
                    
                    // Charts Section
                    HStack(spacing: 16) {
                        // Allocation Pie Chart
                        GlassCard {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("Allocation")
                                    .font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(.secondary)
                                
                                if !viewModel.allocationData.isEmpty {
                                    Chart(viewModel.allocationData) { item in
                                        SectorMark(
                                            angle: .value("Value", item.value),
                                            innerRadius: .ratio(0.618),
                                            angularInset: 1.5
                                        )
                                        .cornerRadius(5)
                                        .foregroundStyle(by: .value("Name", item.label))
                                    }
                                    .frame(height: 150)
                                    .chartLegend(.hidden)
                                } else {
                                    ContentUnavailableView("No Data", systemImage: "chart.pie")
                                        .scaleEffect(0.5)
                                }
                            }
                        }
                        
                        // Performance Info
                        GlassCard {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("Performance")
                                    .font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(.secondary)
                                
                                if let pnl = viewModel.portfolioData?.summary?.totalPnl,
                                   let pnlPercent = viewModel.portfolioData?.summary?.totalPnlPercent {
                                    
                                    Spacer()
                                    VStack(alignment: .leading) {
                                        Text(String(format: "£%.2f", pnl))
                                            .font(.system(size: 20, weight: .black))
                                            .foregroundStyle(pnl >= 0 ? .green : .red)
                                        
                                        Text(String(format: "%@%.2f%%", pnl >= 0 ? "+" : "", pnlPercent))
                                            .font(.system(size: 14, weight: .bold))
                                            .foregroundStyle(pnl >= 0 ? .green : .red)
                                    }
                                    Spacer()
                                } else {
                                    ContentUnavailableView("No Data", systemImage: "chart.line.uptrend.xyaxis")
                                        .scaleEffect(0.5)
                                }
                            }
                        }
                    }
                    .frame(height: 200)
                    .padding(.horizontal)
                    
                    // Account Sections
                    HStack(spacing: 16) {
                        // INVEST Account Section
                        AccountSectionView(
                            title: "INVEST ACCOUNT",
                            accountData: viewModel.investData,
                            isLoading: viewModel.isLoading,
                            onPositionTap: { position in
                                selectedPosition = position
                            }
                        )

                        // ISA Account Section
                        AccountSectionView(
                            title: "ISA ACCOUNT",
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
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(.white.opacity(0.7))
                    .padding(.horizontal)

                if let data = accountData {
                    // Mini metrics for this account
                    VStack(spacing: 8) {
                        HStack(spacing: 8) {
                            MiniMetricCard(
                                title: "Value",
                                value: String(format: "£%.0f", data.summary.currentValue ?? 0),
                                icon: "chart.bar.fill",
                                color: .blue
                            )
                            MiniMetricCard(
                                title: "P&L",
                                value: String(format: "%@£%.0f", (data.summary.totalPnl ?? 0) >= 0 ? "+" : "", data.summary.totalPnl ?? 0),
                                icon: "arrow.up.right.circle.fill",
                                color: (data.summary.totalPnl ?? 0) >= 0 ? .green : .red
                            )
                        }
                        HStack(spacing: 8) {
                            MiniMetricCard(
                                title: "Return",
                                value: {
                                    if let pnl = data.summary.totalPnl, let invested = data.summary.totalInvested, invested > 0 {
                                        let percent = (pnl / invested) * 100
                                        return String(format: "%.1f%%", percent)
                                    } else {
                                        return "N/A"
                                    }
                                }(),
                                icon: "percent",
                                color: .purple
                            )
                            MiniMetricCard(
                                title: "Cash",
                                value: String(format: "£%.0f", data.summary.cashBalance?.free ?? 0),
                                icon: "sterlingsign.circle.fill",
                                color: .orange
                            )
                        }
                    }
                    .padding(.horizontal)

                    // Allocation chart for this account
                    if !data.allocationData.isEmpty {
                        GlassCard {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Allocation")
                                    .font(.system(size: 11, weight: .bold))
                                    .foregroundStyle(.secondary)

                                Chart(data.allocationData) { item in
                                    SectorMark(
                                        angle: .value("Value", item.value),
                                        innerRadius: .ratio(0.618),
                                        angularInset: 1.5
                                    )
                                    .cornerRadius(5)
                                    .foregroundStyle(by: .value("Name", item.label))
                                }
                                .frame(height: 120)
                                .chartLegend(position: .bottom)
                            }
                        }
                    }

                    // Positions for this account
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Holdings")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(.secondary)
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


