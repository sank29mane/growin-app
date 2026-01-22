import Foundation
import SwiftUI
import Combine


class StockChartViewModel: ObservableObject {
    @Published var chartData: [ChartDataPoint] = []
    @Published var isLoading: Bool = false
    @Published var errorMessage: String? = nil
    @Published var selectedTimeframe: String = "1Month"
    @Published var chartTitle: String = ""
    @Published var chartDescription: String = ""
    @Published var aiAnalysis: String = "Loading analysis..."
    @Published var algoSignals: String = "Loading signals..."
    @Published var lastUpdated: Date? = nil
    @Published var realtimePrice: Double? = nil
    @Published var priceChange: Double? = nil
    @Published var priceChangePercent: Double? = nil
    @Published var currency: String = "GBP"
    @Published var market: String = "UK"
    @Published var provider: String = "yfinance"
    @Published var showProviderNotification: Bool = false
    @Published var providerNotificationMessage: String = ""

    let symbol: String
    private let baseURL = "http://127.0.0.1:8002"
    private var cancellables = Set<AnyCancellable>()
    private var webSocketTask: URLSessionWebSocketTask?
    
    init(symbol: String) {
        self.symbol = symbol
        fetchChartData()
        connectWebSocket()
    }

    deinit {
        disconnectWebSocket()
    }
    
    func fetchChartData() {
        isLoading = true
        errorMessage = nil

        let timeframe = selectedTimeframe
        // Try Alpaca first, fallback to yfinance
        guard let url = URL(string: "\(baseURL)/api/chart/\(symbol)?timeframe=\(timeframe)&provider=alpaca") else {
            errorMessage = "Invalid URL"
            isLoading = false
            return
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 15.0  // 15 second timeout

        URLSession.shared.dataTaskPublisher(for: request)
            .map { output -> Data in
                // Debug: Print first 200 bytes of response
                 if let str = String(data: output.data, encoding: .utf8)?.prefix(200) {
                     print("Chart Response: \(str)...")
                 }
                 return output.data
            }
            .handlerError()
            .decode(type: ChartResponse.self, decoder: JSONDecoder())
            .receive(on: DispatchQueue.main)
            .sink { completion in
                self.isLoading = false
                if case .failure(let error) = completion {
                    print("Error fetching chart data: \(error)")
                    // If decoding fails, it might be an empty array or error object
                      if let decodingError = error as? DecodingError {
                         print("Decoding detail: \(decodingError)")
                     }
                     self.errorMessage = error.localizedDescription
                }
            } receiveValue: { response in
                print("Received \(response.data.count) chart points for \(response.metadata.ticker) (\(response.metadata.currency)) via \(response.metadata.provider)")
                self.chartData = response.data
                self.currency = response.metadata.currency
                self.market = response.metadata.market
                self.provider = response.metadata.provider
                self.lastUpdated = Date()

                // Show notification if using fallback provider or if there's an error
                if let error = response.error {
                    // Backend returned error info (e.g., primary provider failed)
                    if error.fallbackUsed {
                        self.showProviderNotification(message: "⚠️ \(error.message)")
                    } else {
                        // All providers failed
                        self.errorMessage = error.message
                        print("Provider errors: \(error.providerErrors.joined(separator: ", "))")
                    }
                } else if (self.market == "UK" && response.metadata.provider != "finnhub") ||
                   (self.market == "US" && response.metadata.provider != "alpaca") {
                    // No explicit error, but using non-preferred provider
                    self.showProviderNotification(message: "Using \(response.metadata.provider) data (primary provider unavailable)")
                }

                self.updateChartMetadata()
            }
            .store(in: &cancellables)
        
        // Also fetch analysis
        fetchAnalysis()
    }
    
    func fetchAnalysis() {
        let timeframe = selectedTimeframe
        guard let url = URL(string: "\(baseURL)/api/analysis/\(symbol)?timeframe=\(timeframe)") else {
            return
        }
        
        var request = URLRequest(url: url)
        request.timeoutInterval = 20.0  // 20 second timeout for agent execution
        
        URLSession.shared.dataTaskPublisher(for: request)
            .map(\.data)
            .decode(type: AnalysisResponse.self, decoder: JSONDecoder())
            .receive(on: DispatchQueue.main)
            .sink { completion in
                if case .failure(let error) = completion {
                    print("Analysis fetch error: \(error)")
                    self.aiAnalysis = "Analysis unavailable"
                    self.algoSignals = "Signals unavailable"
                }
            } receiveValue: { response in
                self.aiAnalysis = response.aiAnalysis
                self.algoSignals = response.algoSignals
                if let updated = response.lastUpdated {
                    self.lastUpdated = ISO8601DateFormatter().date(from: updated)
                }
            }
            .store(in: &cancellables)
    }

    private func showProviderNotification(message: String) {
        self.providerNotificationMessage = message
        self.showProviderNotification = true

        // Auto-hide after 5 seconds
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            self?.showProviderNotification = false
        }
    }

    private func connectWebSocket() {
        guard let url = URL(string: "ws://127.0.0.1:8002/ws/chart/\(symbol)") else {
            print("Invalid WebSocket URL")
            return
        }

        let session = URLSession(configuration: .default)
        webSocketTask = session.webSocketTask(with: url)
        webSocketTask?.resume()

        receiveWebSocketMessages()
    }

    private func disconnectWebSocket() {
        webSocketTask?.cancel(with: .goingAway, reason: nil)
        webSocketTask = nil
    }

    private func receiveWebSocketMessages() {
        webSocketTask?.receive { [weak self] result in
            guard let self = self else { return }

            switch result {
            case .failure(let error):
                print("WebSocket error: \(error)")
                // Attempt to reconnect after delay
                DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
                    self?.connectWebSocket()
                }
            case .success(let message):
                self.handleWebSocketMessage(message)
                // Continue listening for messages
                self.receiveWebSocketMessages()
            }
        }
    }

    private func handleWebSocketMessage(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            guard let data = text.data(using: .utf8) else { return }

            do {
                let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                guard let type = json?["type"] as? String else { return }

                DispatchQueue.main.async {
                    switch type {
                    case "realtime_quote":
                        if let quoteData = json?["data"] as? [String: Any] {
                            self.realtimePrice = quoteData["current_price"] as? Double
                            self.priceChange = quoteData["change"] as? Double
                            self.priceChangePercent = quoteData["change_percent"] as? Double
                            self.lastUpdated = Date()
                        }
                    case "chart_update":
                        if let chartData = json?["data"] as? [[String: Any]],
                           let metadata = json?["metadata"] as? [String: Any] {
                            // Parse chart data points
                            let decoder = JSONDecoder()
                            if let jsonData = try? JSONSerialization.data(withJSONObject: chartData),
                               let points = try? decoder.decode([ChartDataPoint].self, from: jsonData) {
                                self.chartData = points
                                // Update metadata
                                self.currency = metadata["currency"] as? String ?? self.currency
                                self.market = metadata["market"] as? String ?? self.market
                                self.provider = metadata["provider"] as? String ?? self.provider
                                self.lastUpdated = Date()
                            }
                        }
                    case "error":
                        if let errorMsg = json?["message"] as? String {
                            self.errorMessage = errorMsg
                        }
                    default:
                        break
                    }
                }
            } catch {
                print("Failed to parse WebSocket message: \(error)")
            }
        default:
            break
        }
    }

    private func updateChartMetadata() {
        // Set chart title and description based on symbol and timeframe
        let timeframeDescriptions = [
            "1Day": "Intraday",
            "1Week": "Weekly",
            "1Month": "Monthly",
            "3Month": "Quarterly",
            "1Year": "Yearly",
            "Max": "All-time"
        ]

        chartTitle = "\(symbol.uppercased()) - \(timeframeDescriptions[selectedTimeframe] ?? selectedTimeframe) Chart"
        chartDescription = "Historical price data for \(symbol.uppercased()) showing \(selectedTimeframe.lowercased()) timeframe"
    }
    
    func updateTimeframe(_ timeframe: String) {
        selectedTimeframe = timeframe
        updateChartMetadata()
        fetchChartData()
        fetchAnalysis()  // Refresh analysis too
    }
}

struct AnalysisResponse: Codable {
    let aiAnalysis: String
    let algoSignals: String
    let lastUpdated: String?
    
    enum CodingKeys: String, CodingKey {
        case aiAnalysis = "ai_analysis"
        case algoSignals = "algo_signals"
        case lastUpdated = "last_updated"
    }
}

extension Publisher where Output == Data {
    func handlerError() -> AnyPublisher<Data, Error> {
        self.tryMap { data in
            if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let error = json["error"] as? String {
                throw NSError(domain: "API", code: 400, userInfo: [NSLocalizedDescriptionKey: error])
            }
            return data
        }
        .eraseToAnyPublisher()
    }
}
