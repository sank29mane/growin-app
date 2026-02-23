import Foundation
import SwiftUI
import Combine

// MARK: - High Performance Data Processor
actor ChartDataService {
    private let session = URLSession.shared
    
    func fetchChartData(url: URL) async throws -> ChartResponse {
        let (data, _) = try await session.data(from: url)
        let response = try JSONDecoder().decode(ChartResponse.self, from: data)
        return response
    }
    
    /// Downsample data to target count using reliable stride (Visually consistent)
    func downsample(_ data: [ChartDataPoint], target: Int = 1000) -> [ChartDataPoint] {
        guard data.count > target else { return data }
        
        let stride = Double(data.count) / Double(target)
        var result: [ChartDataPoint] = []
        result.reserveCapacity(target)
        
        for i in 0..<target {
            let index = Int(Double(i) * stride)
            if index < data.count {
                result.append(data[index])
            }
        }
        return result
    }
}

// MARK: - WebSocket Manager
private final class WebSocketManager: Sendable {
    let task: URLSessionWebSocketTask
    
    init(url: URL) {
        self.task = URLSession.shared.webSocketTask(with: url)
        self.task.resume()
    }
    
    deinit {
        task.cancel(with: .goingAway, reason: nil)
    }
    
    func receive() async throws -> URLSessionWebSocketTask.Message {
        try await task.receive()
    }
}

// MARK: - WebSocket Message Types
enum WSMessageResult: Sendable {
    case chartInit([ChartDataPoint])
    case chartTick(ChartDataPoint)
    case realtimeQuote(price: Decimal?, change: Decimal?, percent: Decimal?)
    case error(String)
    case unknown
}

private struct WSMessageEnvelope: Codable {
    let type: String
}

private struct WSChartInitMessage: Codable {
    let data: [ChartDataPoint]
}

private struct WSChartTickMessage: Codable {
    let tick: ChartDataPoint
}

private struct WSRealtimeQuoteMessage: Codable {
    struct QuoteData: Codable {
        let current_price: Decimal?
        let change: Decimal?
        let change_percent: Decimal?
    }
    let data: QuoteData
}

private struct WSErrorMessage: Codable {
    let message: String
}

@Observable @MainActor
class StockChartViewModel {
    var chartData: [ChartDataPoint] = [] {
        didSet {
            updateMinMax()
        }
    }

    // Performance optimization: Cache min/max values to avoid O(N) calculation in View body
    var minValue: Decimal = 0
    var maxValue: Decimal = 100

    var isLoading: Bool = false
    var errorMessage: String? = nil
    var selectedTimeframe: String = "1Month"
    var chartTitle: String = ""
    var chartDescription: String = ""
    var aiAnalysis: String = "Loading analysis..."
    var algoSignals: String = "Loading signals..."
    var lastUpdated: Date? = nil
    var realtimePrice: Decimal? = nil
    var priceChange: Decimal? = nil
    var priceChangePercent: Decimal? = nil
    var currency: String = "GBP"
    var market: String = "UK"
    var provider: String = "yfinance"
    var showProviderNotification: Bool = false
    var providerNotificationMessage: String = ""
    
    let symbol: String
    private let config = AppConfig.shared
    private var wsManager: WebSocketManager?
    private let dataService = ChartDataService()
    
    init(symbol: String) {
        self.symbol = symbol
        Task { await fetchChartData() }
        connectWebSocket()
    }
    
    func fetchChartData() async {
        isLoading = true
        errorMessage = nil
        
        let timeframe = selectedTimeframe
        guard let url = URL(string: "\(config.baseURL)/api/chart/\(symbol)?timeframe=\(timeframe)&provider=alpaca") else {
            errorMessage = "Invalid URL"
            isLoading = false
            return
        }
        
        do {
            let response = try await dataService.fetchChartData(url: url)
            let displayData = await dataService.downsample(response.data, target: 800)
            
            withAnimation(.easeInOut) {
                self.chartData = displayData
                self.currency = response.metadata.currency
                self.market = response.metadata.market
                self.provider = response.metadata.provider
                self.lastUpdated = Date()
                self.isLoading = false
            }
                
            if let error = response.error {
                if error.fallbackUsed {
                    self.showProviderNotification(message: "⚠️ \(error.message)")
                } else {
                    self.errorMessage = error.message
                }
            } else if (self.market == "UK" && self.provider == "yfinance") ||
                        (self.market == "US" && self.provider == "yfinance") {
                self.showProviderNotification(message: "ℹ️ Using yfinance (delayed data)")
            }
            
            self.updateChartMetadata()
            await fetchAnalysis()
            
        } catch {
            self.errorMessage = error.localizedDescription
            self.isLoading = false
        }
    }
    
    func fetchAnalysis() async {
        let timeframe = selectedTimeframe
        guard let url = URL(string: "\(config.baseURL)/api/analysis/\(symbol)?timeframe=\(timeframe)") else { return }
        
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let response = try JSONDecoder().decode(AnalysisResponse.self, from: data)
            
            self.aiAnalysis = response.aiAnalysis
            self.algoSignals = response.algoSignals
            if let updated = response.lastUpdated {
                self.lastUpdated = DateUtils.parse(updated)
            }
        } catch {
            print("Analysis fetch error: \(error)")
            self.aiAnalysis = "Analysis unavailable"
            self.algoSignals = "Signals unavailable"
        }
    }
    
    private func showProviderNotification(message: String) {
        self.providerNotificationMessage = message
        self.showProviderNotification = true
        
        Task {
            try? await Task.sleep(for: .seconds(5))
            self.showProviderNotification = false
        }
    }
    
    private func connectWebSocket() {
        guard let url = URL(string: "\(config.webSocketURL)/ws/chart/\(symbol)") else { return }
        self.wsManager = WebSocketManager(url: url)
        listenForMessages()
    }
    
    private func listenForMessages() {
        guard let manager = wsManager else { return }
        Task {
            do {
                while !Task.isCancelled {
                    let message = try await manager.receive()
                    let result = await self.parseWebSocketMessage(message)
                    self.applyWebSocketMessage(result)
                }
            } catch {
                try? await Task.sleep(for: .seconds(5))
                if self.wsManager != nil {
                    self.connectWebSocket()
                }
            }
        }
    }
    
    private func applyWebSocketMessage(_ result: WSMessageResult) {
        switch result {
        case .chartInit(let newPoints):
            self.chartData = newPoints
        case .chartTick(let tick):
            if self.chartData.last?.timestamp != tick.timestamp {
                withAnimation(.linear(duration: 0.5)) {
                    self.chartData.append(tick)
                    if self.chartData.count > 1000 {
                        self.chartData.removeFirst()
                    }
                }
            }
        case .realtimeQuote(let price, let change, let percent):
            self.realtimePrice = price
            self.priceChange = change
            self.priceChangePercent = percent
            self.lastUpdated = Date()
        case .error(let message):
            self.errorMessage = message
        case .unknown:
            break
        }
    }

    nonisolated private func parseWebSocketMessage(_ message: URLSessionWebSocketTask.Message) async -> WSMessageResult {
        switch message {
        case .string(let text):
            guard let data = text.data(using: .utf8) else { return .unknown }
            let decoder = JSONDecoder()
            do {
                let envelope = try decoder.decode(WSMessageEnvelope.self, from: data)
                switch envelope.type {
                case "chart_init":
                    let msg = try decoder.decode(WSChartInitMessage.self, from: data)
                    return .chartInit(msg.data)
                case "chart_tick":
                    let msg = try decoder.decode(WSChartTickMessage.self, from: data)
                    return .chartTick(msg.tick)
                case "realtime_quote":
                    let msg = try decoder.decode(WSRealtimeQuoteMessage.self, from: data)
                    return .realtimeQuote(price: msg.data.current_price,
                                        change: msg.data.change,
                                        percent: msg.data.change_percent)
                case "error":
                    let msg = try decoder.decode(WSErrorMessage.self, from: data)
                    return .error(msg.message)
                default:
                    return .unknown
                }
            } catch {
                return .unknown
            }
        default:
            return .unknown
        }
    }
    
    private func updateMinMax() {
        guard let first = chartData.first else {
            minValue = 0
            maxValue = 100
            return
        }

        var min: Decimal = first.close
        var max: Decimal = first.close

        for point in chartData {
            if point.close < min { min = point.close }
            if point.close > max { max = point.close }
        }

        minValue = min * Decimal(0.99)
        maxValue = max * Decimal(1.01)
    }

    private func updateChartMetadata() {
        let timeframeDescriptions = [
            "1Day": "Intraday", "1Week": "Weekly", "1Month": "Monthly",
            "3Month": "Quarterly", "1Year": "Yearly", "Max": "All-time"
        ]
        chartTitle = "\(symbol.uppercased()) - \(timeframeDescriptions[selectedTimeframe] ?? selectedTimeframe) Chart"
        chartDescription = "Historical price data for \(symbol.uppercased()) showing \(selectedTimeframe.lowercased()) timeframe"
    }
    
    func updateTimeframe(_ timeframe: String) {
        selectedTimeframe = timeframe
        updateChartMetadata()
        Task { await fetchChartData() }
    }
}
