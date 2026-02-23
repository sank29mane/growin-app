import SwiftUI
import Charts

struct StockChartView: View {
    @State var viewModel: StockChartViewModel
    @State private var selectedPoint: ChartDataPoint?
    @State private var chartColor: Color = .green
    
    let timeframes = ["1Day", "1Week", "1Month", "3Month", "1Year", "Max"]
    
    var body: some View {
        ScrollView(showsIndicators: false) {
            VStack(spacing: 24) {
                // Provider notification banner
                if viewModel.showProviderNotification {
                    HStack(spacing: 10) {
                        Image(systemName: "exclamationmark.shield.fill")
                            .foregroundStyle(Color.growinAccent)
                        Text(viewModel.providerNotificationMessage)
                            .font(.system(size: 11, weight: .medium, design: .rounded))
                        Spacer()
                        Button(action: { viewModel.showProviderNotification = false }) {
                            Image(systemName: "xmark")
                                .font(.system(size: 10, weight: .bold))
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Color.growinAccent.opacity(0.1))
                    .clipShape(.rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.growinAccent.opacity(0.2), lineWidth: 0.5))
                    .glassEffect(.thin)
                    .padding(.horizontal)
                    .transition(.move(edge: .top).combined(with: .opacity))
                }

                headerView
                chartContainerView
                timeframePickerView
                
                analysisView
            }
            .padding(.vertical, 24)
        }
        .onAppear {
            updateChartColor()
        }
        .onChange(of: viewModel.chartData) {
            updateChartColor()
        }
    }
    
    @ViewBuilder
    private var headerView: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(viewModel.chartTitle.isEmpty ? viewModel.symbol.uppercased() : viewModel.chartTitle)
                    .font(.system(size: 24, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)

                if !viewModel.chartDescription.isEmpty {
                    Text(viewModel.chartDescription)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(.secondary)
                }
            }

            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 8) {
                    if let selected = selectedPoint {
                        Text(selected.close, format: .currency(code: viewModel.currency))
                            .font(.system(size: 40, weight: .bold, design: .rounded))
                        
                        HStack(spacing: 8) {
                            Text(selected.date, style: .date)
                            Text("•")
                            Text("\(viewModel.market) • \(viewModel.provider)")
                        }
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.secondary.opacity(0.8))
                    } else if let last = viewModel.chartData.last {
                        Text(last.close, format: .currency(code: viewModel.currency))
                            .font(.system(size: 40, weight: .bold, design: .rounded))

                        if let first = viewModel.chartData.first {
                            let change = last.close - first.close
                            let percent = first.close != 0 ? (change / first.close) * 100 : 0

                            HStack(spacing: 6) {
                                Image(systemName: change >= 0 ? "arrow.up.right" : "arrow.down.right")
                                    .font(.system(size: 12, weight: .bold))
                                
                                let changeVal = Double(truncating: change as NSNumber)
                                let percentVal = Double(truncating: percent as NSNumber)
                                Text("\(change >= 0 ? "+" : "")\(String(format: "%.2f", changeVal)) (\(String(format: "%.2f", percentVal))%)")
                            }
                            .font(.system(size: 14, weight: .bold, design: .rounded))
                            .foregroundStyle(change >= 0 ? Color.growinGreen : Color.growinRed)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 4)
                            .background((change >= 0 ? Color.growinGreen : Color.growinRed).opacity(0.1))
                            .clipShape(Capsule())
                            .glassEffect(.thin.interactive())
                        }
                    }
                }

                Spacer()

                HStack(spacing: 12) {
                    PremiumButton(title: "Analyze", icon: "sparkles", color: .growinPrimary) {
                        createNewChatFromChart()
                    }
                    
                    let lastClose = Double(truncating: (viewModel.chartData.last?.close ?? 0) as NSNumber)
                    ShareLink(item: "Intelligence Report: \(viewModel.symbol) Current: \(viewModel.currency) \(String(format: "%.2f", lastClose))") {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 14))
                            .foregroundStyle(.white)
                            .frame(width: 36, height: 36)
                            .background(Color.white.opacity(0.05))
                            .clipShape(Circle())
                            .overlay(Circle().stroke(Color.white.opacity(0.1), lineWidth: 0.5))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.horizontal)
    }
    
    @ViewBuilder
    private var chartContainerView: some View {
        ZStack {
            if viewModel.isLoading {
                ProgressView()
                    .controlSize(.small)
            } else if viewModel.chartData.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 32))
                        .foregroundStyle(.secondary)
                    Text("Intelligence Gap")
                        .font(.system(size: 14, weight: .bold))
                    Text("Historical data for \(viewModel.symbol) is currently unavailable.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding()
            } else {
                chartView
            }
        }
        .frame(height: 280)
        .padding(.vertical)
    }
    
    @ViewBuilder
    private var timeframePickerView: some View {
        HStack(spacing: 8) {
            ForEach(timeframes, id: \.self) { tf in
                Button(action: {
                    withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                        viewModel.updateTimeframe(tf)
                    }
                }) {
                    Text(tfShort(tf))
                        .font(.system(size: 11, weight: .black))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(viewModel.selectedTimeframe == tf ? Color.growinPrimary : Color.white.opacity(0.05))
                        .foregroundStyle(viewModel.selectedTimeframe == tf ? .white : .secondary)
                        .clipShape(Capsule())
                        .overlay(
                            Capsule()
                                .stroke(viewModel.selectedTimeframe == tf ? Color.white.opacity(0.2) : Color.clear, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal)
    }
    
    private var chartView: some View {
        Chart {
            ForEach(viewModel.chartData) { point in
                AreaMark(
                    x: .value("Date", point.date),
                    yStart: .value("Baseline", Double(truncating: viewModel.minValue as NSNumber)),
                    yEnd: .value("Price", Double(truncating: point.close as NSNumber))
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [chartColor.opacity(0.4), chartColor.opacity(0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
                
                LineMark(
                    x: .value("Date", point.date),
                    y: .value("Price", Double(truncating: point.close as NSNumber))
                )
                .foregroundStyle(chartColor)
                .interpolationMethod(.catmullRom)
                .lineStyle(StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
            }
            
            if let selected = selectedPoint {
                RuleMark(x: .value("Selected Date", selected.date))
                    .foregroundStyle(.white.opacity(0.2))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                
                PointMark(
                    x: .value("Selected Date", selected.date),
                    y: .value("Selected Price", Double(truncating: selected.close as NSNumber))
                )
                .foregroundStyle(.white)
                .symbolSize(80)
                .annotation(position: .overlay, alignment: .center) {
                    Circle()
                        .stroke(chartColor, lineWidth: 2)
                        .frame(width: 12, height: 12)
                }
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartYScale(domain: Double(truncating: viewModel.minValue as NSNumber)...Double(truncating: viewModel.maxValue as NSNumber))
        .drawingGroup()
        .chartOverlay { proxy in
            GeometryReader { geometry in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { value in
                                if let plotFrame = proxy.plotFrame {
                                    let x = value.location.x - geometry[plotFrame].origin.x
                                    if let date: Date = proxy.value(atX: x) {
                                        if let closest = findClosestPoint(to: date, in: viewModel.chartData) {
                                            selectedPoint = closest
                                        }
                                    }
                                }
                            }
                            .onEnded { _ in
                                selectedPoint = nil
                            }
                    )
            }
        }
    }

    private func findClosestPoint(to targetDate: Date, in data: [ChartDataPoint]) -> ChartDataPoint? {
        guard !data.isEmpty else { return nil }
        var low = 0
        var high = data.count - 1
        var closest: ChartDataPoint? = nil
        var minDiff: TimeInterval = .infinity
        while low <= high {
            let mid = (low + high) / 2
            let midDate = data[mid].date
            let diff = midDate.timeIntervalSince(targetDate)
            if diff == 0 { return data[mid] }
            else if diff < 0 { low = mid + 1 }
            else { high = mid - 1 }
        }
        let candidates = [high, low]
        for idx in candidates {
            if idx >= 0 && idx < data.count {
                let diff = abs(data[idx].date.timeIntervalSince(targetDate))
                if diff < minDiff {
                    minDiff = diff
                    closest = data[idx]
                }
            }
        }
        return closest
    }
    
    private func updateChartColor() {
        guard let first = viewModel.chartData.first, let last = viewModel.chartData.last else { return }
        chartColor = last.close >= first.close ? Color.growinGreen : Color.growinRed
    }
    
    private func tfShort(_ tf: String) -> String {
        switch tf {
        case "1Day": return "1D"
        case "1Week": return "1W"
        case "1Month": return "1M"
        case "3Month": return "3M"
        case "1Year": return "1Y"
        case "Max": return "ALL"
        default: return tf
        }
    }

    @ViewBuilder
    private var analysisView: some View {
        VStack(spacing: 16) {
            HStack(spacing: 16) {
                GlassCard(cornerRadius: 20) {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Label("NEURAL INSIGHT", systemImage: "brain.headset")
                                .font(.system(size: 10, weight: .black))
                                .foregroundStyle(Color.Persona.analyst)
                            Spacer()
                            if let updated = viewModel.lastUpdated {
                                Text(updated, style: .relative)
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Text(viewModel.aiAnalysis)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(.white.opacity(0.8))
                            .lineSpacing(2)
                    }
                }
                
                GlassCard(cornerRadius: 20) {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Label("QUANT VECTORS", systemImage: "bolt.shield.fill")
                                .font(.system(size: 10, weight: .black))
                                .foregroundStyle(Color.growinAccent)
                            Spacer()
                            if let updated = viewModel.lastUpdated {
                                Text(updated, style: .relative)
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Text(viewModel.algoSignals)
                            .font(.system(size: 12, weight: .medium, design: .rounded))
                            .foregroundStyle(.white.opacity(0.8))
                            .lineSpacing(2)
                    }
                }
            }
        }
        .padding(.horizontal)
    }

    private func createNewChatFromChart() {
        let lastClose = Double(truncating: (viewModel.chartData.last?.close ?? 0) as NSNumber)
        let chartContext: [String: Any] = [
            "symbol": viewModel.symbol,
            "timeframe": viewModel.selectedTimeframe,
            "currentPrice": lastClose,
            "chartTitle": viewModel.chartTitle
        ]
        NotificationCenter.default.post(name: NSNotification.Name("CreateChatFromChart"), object: nil, userInfo: chartContext)
    }
}
