import SwiftUI
import Charts

struct PortfolioView: View {
    @Bindable var viewModel: PortfolioViewModel
    
    var body: some View {
        VStack(spacing: 0) {
            // Main Content Area
            ScrollView(showsIndicators: false) {
                VStack(spacing: 32) {
                    // Integrated Navigation Header
                    HStack(alignment: .bottom) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Portfolio")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            
                            Text("Overview")
                                .font(.title)
                                .fontWeight(.bold)
                                .foregroundStyle(.primary)
                        }
                        
                        Spacer()
                        
                        // Premium Account Toggle
                        HStack(spacing: 0) {
                            let defaults = UserDefaults.standard
                            ForEach(["invest", "isa"], id: \.self) { type in
                                Button(action: {
                                    Task { await viewModel.switchAccount(newType: type) }
                                }) {
                                    Text(type == "invest" ? "General" : "ISA")
                                        .font(.caption)
                                        .fontWeight(.semibold)
                                        .padding(.horizontal, 16)
                                        .padding(.vertical, 8)
                                        .background(defaults.string(forKey: "t212AccountType") == type ? Color.white.opacity(0.1) : Color.clear)
                                        .foregroundStyle(defaults.string(forKey: "t212AccountType") == type ? .primary : .secondary)
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel(type == "invest" ? "General Account" : "ISA Account")
                                .accessibilityHint("Switches portfolio view to \(type == "invest" ? "General" : "ISA") account")
                                .accessibilityAddTraits(defaults.string(forKey: "t212AccountType") == type ? [.isSelected] : [])
                            }
                        }
                        .background(Color.white.opacity(0.05))
                        .clipShape(Capsule())
                        .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
                        .disabled(viewModel.isSwitchingAccount)
                        
                        Button(action: { Task { await viewModel.fetchPortfolio() } }) {
                            Image(systemName: "arrow.clockwise")
                                .font(.system(size: 12, weight: .bold))
                                .frame(width: 32, height: 32)
                                .background(Color.white.opacity(0.05))
                                .clipShape(Circle())
                        }
                        .buttonStyle(.plain)
                        .opacity(viewModel.isLoading ? 0.5 : 1)
                        .accessibilityLabel("Refresh Portfolio")
                        .accessibilityHint("Refreshes portfolio data from the server")
                        .disabled(viewModel.isLoading)
                    }
                    .padding(.horizontal)
                    .padding(.top, 24)

                    if let snapshot = viewModel.snapshot {
                        analyticsSection(snapshot: snapshot)
                        holdingsSection(snapshot: snapshot)
                    } else if !viewModel.isLoading {
                        offlineView
                    }
                }
                .padding(.bottom, 40)
            }
        }
        .sheet(item: $viewModel.selectedPosition) { position in
            if let ticker = position.ticker {
                NavigationStack {
                    StockChartView(viewModel: StockChartViewModel(symbol: ticker))
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Close") {
                                    viewModel.selectedPosition = nil
                                }
                            }
                        }
                }
                .frame(minWidth: 800, minHeight: 600)
            }
        }
        .onAppear {
            Task {
                await viewModel.onAppear()
            }
        }
        .onChange(of: viewModel.selectedTimeRange) { _, _ in
            Task { await viewModel.fetchHistory() }
        }
    }
}

struct MetricGrid: View {
    let summary: PortfolioSummary?
    
    var body: some View {
        VStack(spacing: 16) {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 16) {
                MiniMetricCard(title: "Total Capital", value: String(format: "£%.2f", (summary?.currentValue ?? 0) + (summary?.cashBalance?.free ?? 0)), icon: "banknote.fill", color: .growinAccent)
                MiniMetricCard(title: "Equity", value: String(format: "£%.2f", summary?.currentValue ?? 0), icon: "chart.bar.fill", color: .growinPrimary)
                MiniMetricCard(title: "Net Profit", value: String(format: "£%.2f", summary?.totalPnl ?? 0), icon: "waveform.path.ecg", color: (summary?.totalPnl ?? 0) >= 0 ? .growinGreen : .growinRed)
                MiniMetricCard(title: "ROI", value: String(format: "%.2f%%", summary?.totalPnlPercent ?? 0), icon: "percent", color: .white)
                MiniMetricCard(title: "Cash", value: String(format: "£%.2f", summary?.cashBalance?.free ?? 0), icon: "pouch.fill", color: .growinOrange)
            }
            
            if let accounts = summary?.accounts, accounts.count > 1 {
                HStack(spacing: 8) {
                    ForEach(Array(accounts.keys).sorted(), id: \.self) { key in
                        if let acc = accounts[key] {
                            HStack(spacing: 4) {
                                Text(key.uppercased())
                                    .font(.system(size: 8, weight: .black))
                                    .foregroundStyle(.secondary)
                                Text(String(format: "£%.2f", acc.currentValue ?? 0))
                                    .font(.system(size: 10, weight: .bold))
                            }
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(Color.white.opacity(0.05))
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
                        }
                    }
                }
            }
        }
    }
}

struct MiniMetricCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color
    @State private var isHovered = false
    
    var body: some View {
        GlassCard(cornerRadius: 20) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Image(systemName: icon)
                        .foregroundStyle(color)
                        .font(.system(size: 14))
                    Text(title)
                        .font(.caption2)
                        .fontWeight(.bold)
                        .foregroundStyle(.secondary)
                }
                
                Text(value)
                    .font(.system(size: 20, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title): \(value)")
    }
}

struct AllocationDonutChart: View {
    let positions: [Position]
    
    var body: some View {
        Chart {
            ForEach(topFiveAllocation) { item in
                SectorMark(
                    angle: .value("Value", item.value),
                    innerRadius: .ratio(0.65),
                    angularInset: 2
                )
                .clipShape(.rect(cornerRadius: 4))
                .foregroundStyle(by: .value("Ticker", item.label))
            }
        }
        .chartLegend(position: .trailing)
    }
    
    private var topFiveAllocation: [AllocationItem] {
        let items = positions.map { pos in
            AllocationItem(label: pos.ticker ?? "???", value: (pos.currentPrice ?? 0) * (pos.quantity ?? 0))
        }.sorted { $0.value > $1.value }
        
        var result = Array(items.prefix(5))
        if items.count > 5 {
            let othersValue = items.dropFirst(5).reduce(0) { $0 + $1.value }
            result.append(AllocationItem(label: "Others", value: othersValue))
        }
        return result
    }
}

struct PositionDeepCard: View {
    let position: Position
    
    var body: some View {
        GlassCard(cornerRadius: 14) {
            HStack(spacing: 16) {
                VStack(spacing: 4) {
                    Circle()
                        .fill(Color.blue.opacity(0.1))
                        .frame(width: 36, height: 36)
                        .overlay(
                            Text(position.ticker?.prefix(1) ?? "?")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(.blue)
                        )
                    
                    if let acc = position.accountType {
                        Text(acc.uppercased())
                            .font(.system(size: 6, weight: .black))
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                            .background(acc == "isa" ? Color.purple.opacity(0.2) : Color.blue.opacity(0.2))
                            .foregroundStyle(acc == "isa" ? .purple : .blue)
                            .clipShape(.rect(cornerRadius: 3))
                    }
                }
                
                VStack(alignment: .leading, spacing: 4) {
                    Text(position.name ?? position.ticker ?? "UNKNOWN")
                        .font(.system(size: 16, weight: .black))
                        .foregroundStyle(.white)
                    
                    if position.name != nil {
                        Text(position.ticker ?? "")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.white.opacity(0.4))
                    }
                    
                    Text("\(String(format: "%.2f", position.quantity ?? 0)) shares")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.white.opacity(0.5))
                }
                
                Spacer()
                
                VStack(alignment: .trailing, spacing: 4) {
                    let value = (position.currentPrice ?? 0) * (position.quantity ?? 0)
                    Text(String(format: "£%.2f", value))
                        .font(.system(size: 16, weight: .black))
                        .foregroundStyle(.white)
                    
                    let pnl = position.ppl ?? 0
                    Text(String(format: "%@£%.2f", pnl >= 0 ? "+" : "", pnl))
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(pnl >= 0 ? .green : .red)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityString)
    }

    private var accessibilityString: String {
        let name = position.name ?? position.ticker ?? "Unknown Position"
        let shareCount = position.quantity ?? 0
        let totalValue = (position.currentPrice ?? 0) * shareCount
        let pnlVal = position.ppl ?? 0

        return "\(name). \(String(format: "%.2f", shareCount)) shares. Value £\(String(format: "%.2f", totalValue)). \(pnlVal >= 0 ? "Profit" : "Loss") £\(String(format: "%.2f", abs(pnlVal)))."
    }
}

// MARK: - Supporting Types

extension PortfolioView {
    private var timeRangePicker: some View {
        HStack(spacing: 8) {
            ForEach([TimeRange.day, .week, .month, .threeMonths, .year, .all], id: \.self) { range in
                Button(action: {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                        viewModel.selectedTimeRange = range
                    }
                }) {
                    Text(rangeTitle(for: range))
                        .font(.system(size: 9, weight: .bold))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(viewModel.selectedTimeRange == range ? Color.growinAccent.opacity(0.2) : Color.white.opacity(0.05))
                        .foregroundStyle(viewModel.selectedTimeRange == range ? Color.growinAccent : .secondary)
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(accessibilityRangeTitle(for: range))
                .accessibilityHint("Shows performance history for this period")
                .accessibilityAddTraits(viewModel.selectedTimeRange == range ? [.isSelected] : [])
            }
        }
    }
    
    private func rangeTitle(for range: TimeRange) -> String {
        switch range {
        case .day: return "1D"
        case .week: return "1W"
        case .month: return "1M"
        case .threeMonths: return "3M"
        case .year: return "1Y"
        case .all: return "ALL"
        }
    }

    private func accessibilityRangeTitle(for range: TimeRange) -> String {
        switch range {
        case .day: return "1 Day"
        case .week: return "1 Week"
        case .month: return "1 Month"
        case .threeMonths: return "3 Months"
        case .year: return "1 Year"
        case .all: return "All Time"
        }
    }
}



struct IntelligenceOfflineView: View {
    let errorMessage: String?
    let retryAction: () -> Void
    
    var body: some View {
        GlassCard(cornerRadius: 32) {
            VStack(spacing: 24) {
                Image(systemName: "bolt.horizontal.icloud.fill")
                    .font(.system(size: 44))
                    .foregroundStyle(
                        LinearGradient(colors: [.growinAccent, .growinPrimary], startPoint: .topLeading, endPoint: .bottomTrailing)
                    )
                    .shadow(color: Color.growinAccent.opacity(0.5), radius: 10)
                
                VStack(spacing: 8) {
                    Text("Connection Error")
                        .font(.title3)
                        .fontWeight(.semibold)
                    Text(errorMessage ?? "Unable to connect to the server.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                
                Button(action: retryAction) {
                    HStack {
                        Image(systemName: "arrow.triangle.2.circlepath")
                        Text("Retry")
                    }
                    .font(.body)
                    .fontWeight(.medium)
                    .padding(.horizontal, 24)
                    .padding(.vertical, 12)
                    .background(Color.growinPrimary)
                    .clipShape(Capsule())
                    .foregroundStyle(.white)
                }
                .buttonStyle(.plain)
            }
            .padding(40)
        }
        .padding(40)
    }
}

struct GradientBackground: View {
    var body: some View {
        Color.growinDarkBg.ignoresSafeArea()
    }
}

struct PerformanceLineChart: View {
    let history: [PortfolioHistoryPoint]
    let timeRange: TimeRange
    
    var body: some View {
        if history.isEmpty {
            ContentUnavailableView("No Data", systemImage: "chart.line.uptrend.xyaxis")
                .scaleEffect(0.5)
        } else {
            let values = history.map { $0.totalValue }
            let rawMin = values.min() ?? 0
            let rawMax = values.max() ?? 0
            
            let range = rawMax - rawMin
            let padding = range > 0 ? range * 0.1 : max(rawMin * 0.1, 10)
            let minValue = rawMin - padding
            let maxValue = rawMax + padding
            
            let dates = history.map { $0.date }
            let minDate = dates.min() ?? Date()
            let maxDate = dates.max() ?? Date()
            
            Chart(history) { point in
                LineMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(Color.growinAccent)
                .lineStyle(StrokeStyle(lineWidth: 3))
                .interpolationMethod(.catmullRom)
                
                AreaMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [Color.growinAccent.opacity(0.3), Color.growinAccent.opacity(0.0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
            }
            .chartYAxis {
                AxisMarks(position: .leading) { value in
                    AxisGridLine(stroke: StrokeStyle(dash: [2, 4])).foregroundStyle(.white.opacity(0.1))
                    AxisValueLabel() {
                        if let doubleValue = value.as(Double.self) {
                            Text(doubleValue, format: .currency(code: "GBP").precision(.fractionLength(0)))
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 5)) { value in
                    AxisGridLine(stroke: StrokeStyle(dash: [2, 4])).foregroundStyle(.white.opacity(0.1))
                    AxisValueLabel() {
                        if let dateValue = value.as(Date.self) {
                            Text(dateValue, format: .dateTime.month().day())
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .chartYScale(domain: minValue...maxValue)
            .chartXScale(domain: minDate...maxDate)
        }
    }
}

extension PortfolioView {
    // MARK: - Subviews
    
    private var headerView: some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Portfolio")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.secondary)
                
                Text("Overview")
                    .font(.title)
                    .fontWeight(.bold)
                    .foregroundStyle(.primary)
            }
            
            Spacer()
            
            // Premium Account Toggle
            HStack(spacing: 0) {
                let defaults = UserDefaults.standard
                ForEach(["invest", "isa"], id: \.self) { type in
                    Button(action: {
                        Task { await viewModel.switchAccount(newType: type) }
                    }) {
                        Text(type == "invest" ? "General" : "ISA")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(defaults.string(forKey: "t212AccountType") == type ? Color.white.opacity(0.1) : Color.clear)
                            .foregroundStyle(defaults.string(forKey: "t212AccountType") == type ? .primary : .secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(type == "invest" ? "General Account" : "ISA Account")
                    .accessibilityHint("Switches portfolio view to \(type == "invest" ? "General" : "ISA") account")
                    .accessibilityAddTraits(defaults.string(forKey: "t212AccountType") == type ? [.isSelected] : [])
                }
            }
            .background(Color.white.opacity(0.05))
            .clipShape(Capsule())
            .overlay(Capsule().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
            .disabled(viewModel.isSwitchingAccount)
            
            Button(action: { Task { await viewModel.fetchPortfolio() } }) {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .bold))
                    .frame(width: 32, height: 32)
                    .background(Color.white.opacity(0.05))
                    .clipShape(Circle())
            }
            .buttonStyle(.plain)
            .opacity(viewModel.isLoading ? 0.5 : 1)
            .accessibilityLabel("Refresh Portfolio")
            .accessibilityHint("Refreshes portfolio data from the server")
            .disabled(viewModel.isLoading)
        }
        .padding(.horizontal)
        .padding(.top, 24)
    }

    private func analyticsSection(snapshot: PortfolioSnapshot) -> some View {
        // Analytics Engine Block
        VStack(spacing: 24) {
            MetricGrid(summary: snapshot.summary)
            
            HStack(spacing: 20) {
                // Performance Perspective
                if !viewModel.portfolioHistory.isEmpty {
                    GlassCard(cornerRadius: 24) {
                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                Label("Performance", systemImage: "waveform.path.ecg")
                                    .font(.caption)
                                    .fontWeight(.semibold)
                                    .foregroundStyle(.secondary)
                                
                                Spacer()
                                
                                timeRangePicker
                            }
                            
                            PerformanceLineChart(history: viewModel.portfolioHistory, timeRange: viewModel.selectedTimeRange)
                                .frame(height: 200)
                        }
                    }
                }
                
                // Allocation Vectors
                if let positions = snapshot.positions, !positions.isEmpty {
                    GlassCard(cornerRadius: 24) {
                        VStack(alignment: .leading, spacing: 16) {
                            Label("Allocation", systemImage: "chart.pie.fill")
                                .font(.caption)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            
                            AllocationDonutChart(positions: positions)
                                .frame(height: 200)
                        }
                    }
                    .frame(width: 320)
                }
            }
        }
        .padding(.horizontal)
    }

    private func holdingsSection(snapshot: PortfolioSnapshot) -> some View {
        // Strategic Holdings
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Text("Holdings")
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.secondary)
                
                Spacer()
                
                Text("\(snapshot.positions?.count ?? 0) ACTIVE")
                    .font(.caption2)
                    .fontWeight(.bold)
                    .foregroundStyle(Color.growinPrimary)
            }
            .padding(.horizontal)
            
            LazyVStack(spacing: 12) {
                ForEach(snapshot.positions ?? []) { position in
                    PositionDeepCard(position: position)
                        .glassEffect(.thin.interactive(), in: .rect(cornerRadius: 14))
                        .onTapGesture {
                            viewModel.selectedPosition = position
                        }
                }
            }
            .padding(.horizontal)
        }
        .padding(.top, 8)
    }
    
    private var offlineView: some View {
        IntelligenceOfflineView(errorMessage: viewModel.errorMsg) {
            Task {
                await viewModel.fetchPortfolio()
                await viewModel.fetchHistory()
            }
        }
    }
}

