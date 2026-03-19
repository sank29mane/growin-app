import Foundation
import Combine
import SwiftUI

struct AlphaPoint: Identifiable, Sendable {
    let id = UUID()
    let timestamp: Date
    let portfolioValue: Double
    let benchmarkValue: Double
    let alpha: Double
}

struct DisplayPoint: Sendable {
    let x: CGFloat
    let yPortfolio: CGFloat
    let yBenchmark: CGFloat
}

actor AlphaCoordinateTransformer {
    func transform(points: [AlphaPoint], size: CGSize) -> [DisplayPoint] {
        guard points.count > 1 else { return [] }
        let portfolioValues = points.map { $0.portfolioValue }
        let benchmarkValues = points.map { $0.benchmarkValue }
        let minVal = min(portfolioValues.min() ?? 0, benchmarkValues.min() ?? 0)
        let maxVal = max(portfolioValues.max() ?? 1, benchmarkValues.max() ?? 1)
        let range = max(maxVal - minVal, 0.0001)
        let width = size.width
        let height = size.height
        let count = points.count
        return points.enumerated().map { index, point in
            let x = CGFloat(index) / CGFloat(count - 1) * width
            let yPortfolio = height - CGFloat((point.portfolioValue - minVal) / range) * height
            let yBenchmark = height - CGFloat((point.benchmarkValue - minVal) / range) * height
            return DisplayPoint(x: x, yPortfolio: yPortfolio, yBenchmark: yBenchmark)
        }
    }
}

@Observable
class AlphaStreamViewModel {
    var points: [AlphaPoint] = []
    var displayPoints: [DisplayPoint] = []
    var currentRegime: String = "IDLE"
    var isConnected: Bool = false
    var lastAlpha: Double = 0.0
    private var webSocketTask: URLSessionWebSocketTask?
    private let transformer = AlphaCoordinateTransformer()
    private var lastSize: CGSize = .zero
    private let streamURL = URL(string: "ws://localhost:8000/api/alpha/stream")!
    func connect() {
        guard !isConnected else { return }
        let session = URLSession(configuration: .default)
        webSocketTask = session.webSocketTask(with: streamURL)
        webSocketTask?.resume()
        isConnected = true
        receiveMessage()
    }
    func disconnect() {
        webSocketTask?.cancel(with: .goingAway, reason: nil)
        isConnected = false
    }
    private func receiveMessage() {
        webSocketTask?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let text): self.handleIncomingData(text)
                case .data(let data): if let text = String(data: data, encoding: .utf8) { self.handleIncomingData(text) }
                @unknown default: break
                }
                if self.isConnected { self.receiveMessage() }
            case .failure(let error):
                print("WebSocket Error: \(error)")
                Task { @MainActor in
                    self.isConnected = false
                    self.currentRegime = "OFFLINE"
                }
            }
        }
    }
    private func handleIncomingData(_ jsonString: String) {
        guard let data = jsonString.data(using: .utf8) else { return }
        do {
            let decoder = JSONDecoder()
            let response = try decoder.decode(AlphaStreamResponse.self, from: data)
            Task { @MainActor in
                let newPoint = AlphaPoint(
                    timestamp: Date(timeIntervalSince1970: response.timestamp),
                    portfolioValue: response.portfolio_val,
                    benchmarkValue: response.benchmark_val,
                    alpha: response.alpha
                )
                self.points.append(newPoint)
                if self.points.count > 120 { self.points.removeFirst() }
                self.currentRegime = response.regime
                self.lastAlpha = response.alpha
                if self.lastSize != .zero { await self.updateDisplayCoordinates(size: self.lastSize) }
                if (response.conviction ?? 0) > 0.95 || response.alpha > 0.08 {
                     NotificationManager.shared.scheduleTradeSuggestion(ticker: "FTSE/GROW", action: "Rebalance", confidence: 0.98)
                }
            }
        } catch { print("Decode Error: \(error)") }
    }
    func updateDisplayCoordinates(size: CGSize) async {
        self.lastSize = size
        let currentPoints = self.points
        let newDisplayPoints = await transformer.transform(points: currentPoints, size: size)
        await MainActor.run { self.displayPoints = newDisplayPoints }
    }
}
struct AlphaStreamResponse: Codable {
    let portfolio_val: Double
    let benchmark_val: Double
    let alpha: Double
    let regime: String
    let conviction: Double?
    let timestamp: Double
}
