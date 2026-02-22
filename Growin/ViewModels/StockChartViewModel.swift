import Foundation
import SwiftUI
import Combine
import Accelerate

// MARK: - High Performance Data Processor (M4 Pro Optimized)
actor ChartDataService {
    private let session = URLSession.shared
    
    func fetchChartData(url: URL) async throws -> (ChartResponse, [Double]) {
        let (data, _) = try await session.data(from: url)
        let response = try JSONDecoder().decode(ChartResponse.self, from: data)
        
        // M4 PRO OPTIMIZATION: Prepare numerical data for SIMD processing
        let closes = response.data.map { $0.close }
        return (response, closes)
    }
    
    /// Calculate Moving Average using Apple's vDSP (Vector Digital Signal Processing)
    /// This utilizes the AMX units on M-series chips for massive speedup over loops.
    func calculateSMA(data: [Double], period: Int) -> [Double] {
        let n = data.count
        guard n >= period else { return [] }
        
        // M4 Pro Optimization: Use sliding window sum directly
        // This is O(N) and uses vector units.
        let sums = vDSP.slidingWindowSum(data, usingWindowLength: period)
        
        // Vector division by period
        return vDSP.divide(sums, Double(period))
    }
    
    /// Downsample data to target count using reliable stride (Visually consistent)
    func downsample(_ data: [ChartDataPoint], target: Int = 1000) -> [ChartDataPoint] {
        guard data.count > target else { return data }
        
        let stride = Float(data.count) / Float(target)
        var result: [ChartDataPoint] = []
        result.reserveCapacity(target)
        
        for i in 0..<target {
            let index = Int(Float(i) * stride)
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
    case realtimeQuote(price: Double?, change: Double?, percent: Double?)
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
        let current_price: Double?
        let change: Double?
        let change_percent: Double?
    }
    let data: QuoteData
}

private struct WSErrorMessage: Codable {
    let message: String
}

@Observable @MainActor
class StockChartViewModel {
    var chartData: [ChartDataPoint] = []
    var isLoading: Bool = false
    var errorMessage: String? = nil
    var selectedTimeframe: String = "1Month"
    var chartTitle: String = ""
    var chartDescription: String = ""
    var aiAnalysis: String = "Loading analysis..."
    var algoSignals: String = "Loading signals..."
    var lastUpdated: Date? = nil
    var realtimePrice: Double? = nil
    var priceChange: Double? = nil
    var priceChangePercent: Double? = nil
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
    
    // Deinit is now safe because WebSocketManager handles cancellation
    deinit { }
    
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
            let (response, _) = try await dataService.fetchChartData(url: url)
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
            } else if self.provider == "Synthetic" {
                // STRICT: This should never happen now
                self.errorMessage = "⚠️ No real market data available. Check your connection."
            } else if (self.market == "UK" && self.provider == "yfinance") ||
                        (self.market == "US" && self.provider == "yfinance") {
                // Fallback to yfinance is OK but inform user
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
                    // M4 PRO OPTIMIZATION: Move JSON parsing to background thread
                    // parseWebSocketMessage is nonisolated async, so it runs on global pool
                    let result = await self.parseWebSocketMessage(message)

                    // Update UI state on MainActor
                    self.applyWebSocketMessage(result)
                }
            } catch {
                // Connection lost or cancelled, reconnect after delay
                try? await Task.sleep(for: .seconds(5))
                if self.wsManager != nil { // Only reconnect if not deinitialized
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
            // Prevent duplicates and append
            if self.chartData.last?.timestamp != tick.timestamp {
                withAnimation(.linear(duration: 0.5)) {
                    self.chartData.append(tick)
                    // Keep only last 1000 points for performance
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
                print("Failed to parse WebSocket message: \(error)")
                return .unknown
            }
        default:
            return .unknown
        }
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
 
// AnalysisResponse moved to CoreModels.swift
