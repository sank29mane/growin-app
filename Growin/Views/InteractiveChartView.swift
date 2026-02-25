import SwiftUI
import Charts
import Foundation

struct InteractiveChartView: View {
    let history: [TimeSeriesItem]
    let forecast: [TimeSeriesItem]
    let ticker: String
    let showTTMIndicator: Bool = true

    @State private var selectedDate: Date? = nil
    @State private var selectedPrice: Decimal? = nil
    @State private var selectedType: String? = nil
    @State private var sortedData: [(Date, Decimal, String)] = []

    // TTM Model metadata
    var hasTTMData: Bool {
        !forecast.isEmpty
    }

    private func updateSortedData() {
        var result: [(Date, Decimal, String)] = []

        let historyPoints = history.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close, "Historical")
        }
        result.append(contentsOf: historyPoints)

        let forecastPoints = forecast.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close, "TTM Forecast")
        }
        result.append(contentsOf: forecastPoints)

        // Pre-sort once when data changes
        result.sort { $0.0 < $1.0 }
        self.sortedData = result
    }

    private var headerView: some View {
        HStack {
            VStack(alignment: .leading) {
                Text("\(ticker) Price Analysis")
                    .font(.headline)
                    .foregroundStyle(.white)

                if let price = selectedPrice, let date = selectedDate {
                    Text("\(price.formatted(.number.precision(.fractionLength(2)))) at \(date, format: .dateTime.hour().minute().day())")
                        .font(.caption)
                        .foregroundStyle(.blue)
                } else {
                    HStack(spacing: 4) {
                        Text("Historical + TTM-R2 Forecast")
                            .font(.caption)
                            .foregroundStyle(.gray)
                        if showTTMIndicator {
                            Image(systemName: "brain")
                                .font(.caption2)
                                .foregroundStyle(.green.opacity(0.8))
                        }
                    }
                }
            }

            Spacer()

            // Enhanced Legend with TTM indicator
            HStack(spacing: 12) {
                LegendItem(label: "History", color: Color.blue, dashed: false)
                LegendItem(label: "TTM-R2 Forecast", color: Color.green, dashed: true)
                if showTTMIndicator && hasTTMData {
                    HStack(spacing: 4) {
                        Image(systemName: "brain.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                        Text("AI")
                            .font(.caption2)
                            .fontWeight(.bold)
                            .foregroundStyle(.green)
                    }
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.green.opacity(0.1))
                    .cornerRadius(8)
                }
            }
        }
        .padding(.horizontal, 4)
    }

    private var chartView: some View {
        Chart {
            // Historical Line
            ForEach(history) { item in
                LineMark(
                    x: .value("Time", Date(timeIntervalSince1970: Double(item.timestamp) / 1000.0)),
                    y: .value("Price", Double(truncating: item.close as NSNumber)),
                    series: .value("Type", "History")
                )
                .foregroundStyle(Color.blue)
                .interpolationMethod(.catmullRom)
            }

            // TTM-R2 Forecast Line
            ForEach(forecast) { item in
                LineMark(
                    x: .value("Time", Date(timeIntervalSince1970: Double(item.timestamp) / 1000.0)),
                    y: .value("Price", Double(truncating: item.close as NSNumber)),
                    series: .value("Type", "TTM Forecast")
                )
                .foregroundStyle(Color.green)
                .lineStyle(StrokeStyle(lineWidth: 3, dash: [8, 4]))
                .interpolationMethod(.catmullRom)
            }

            // Transition marker
            if let lastHistoricalDate = history.last?.timestamp,
               let firstForecastDate = forecast.first?.timestamp,
               lastHistoricalDate < firstForecastDate {
                RuleMark(x: .value("Transition", Date(timeIntervalSince1970: Double(lastHistoricalDate) / 1000.0)))
                    .foregroundStyle(.orange.opacity(0.5))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 2]))
                    .annotation(position: .top, alignment: .leading) {
                        Text("TTM-R2")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                            .background(.black.opacity(0.7))
                            .cornerRadius(3)
                    }
            }

            // Selection Rule
            if let selectedDate = selectedDate {
                RuleMark(x: .value("Selected", selectedDate))
                    .foregroundStyle(.white.opacity(0.3))
                    .offset(yStart: -10)
                    .annotation(position: .top) {
                        if let price = selectedPrice {
                            let isTTMData = selectedType == "TTM Forecast"
                            VStack(spacing: 2) {
                                Text(price.formatted(.number.precision(.fractionLength(2))))
                                    .font(.caption)
                                    .fontWeight(.bold)
                                Text(isTTMData ? "TTM-R2 Forecast" : "Historical")
                                    .font(.caption2)
                                    .foregroundStyle(isTTMData ? .green : .blue)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.ultraThinMaterial)
                            .cornerRadius(6)
                            .shadow(radius: 2)
                        }
                    }
            }
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .day)) { value in
                AxisGridLine()
                AxisValueLabel(format: .dateTime.day().month())
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine()
                AxisValueLabel()
            }
        }
        .chartYScale(domain: .automatic(includesZero: false))
        .frame(height: 200)
        .padding(.top, 8)
        .chartOverlay { proxy in
            GeometryReader { geometry in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(
                        DragGesture()
                            .onChanged { value in
                                guard let plotFrame = proxy.plotFrame else { return }
                                let origin = geometry[plotFrame].origin
                                let location = CGPoint(
                                    x: value.location.x - origin.x,
                                    y: value.location.y - origin.y
                                )

                                if let date: Date = proxy.value(atX: location.x) {
                                    selectedDate = date
                                    resolvePrice(at: date)
                                }
                            }
                            .onEnded { _ in
                                selectedDate = nil
                                selectedPrice = nil
                                selectedType = nil
                            }
                    )
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerView

            chartView
        }
        .padding(16)
        .background {
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
                .overlay {
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(.white.opacity(0.1), lineWidth: 1)
                }
        }
        .onAppear {
            updateSortedData()
        }
        .onChange(of: history) { _, _ in
            updateSortedData()
        }
        .onChange(of: forecast) { _, _ in
            updateSortedData()
        }
    }
    
    private func resolvePrice(at date: Date) {
        if sortedData.isEmpty { return }
        
        var l = 0
        var r = sortedData.count
        while l < r {
            let mid = l + (r - l) / 2
            if sortedData[mid].0 >= date {
                r = mid
            } else {
                l = mid + 1
            }
        }

        var closestIndex = 0
        if l == 0 {
            closestIndex = 0
        } else if l == sortedData.count {
            closestIndex = sortedData.count - 1
        } else {
            let before = sortedData[l - 1]
            let after = sortedData[l]
            if abs(before.0.timeIntervalSince(date)) < abs(after.0.timeIntervalSince(date)) {
                closestIndex = l - 1
            } else {
                closestIndex = l
            }
        }

        let item = sortedData[closestIndex]
        selectedPrice = item.1
        selectedType = item.2
    }
}

struct LegendItem: View {
    let label: String
    let color: Color
    let dashed: Bool
    
    var body: some View {
        HStack(spacing: 4) {
            if dashed {
                Capsule()
                    .stroke(color, style: StrokeStyle(lineWidth: 2, dash: [2, 2]))
                    .frame(width: 12, height: 4)
            } else {
                Capsule()
                    .fill(color)
                    .frame(width: 12, height: 4)
            }
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.gray)
        }
    }
}
