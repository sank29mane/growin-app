import Foundation
import Combine
import SwiftUI

class DashboardViewModel: ObservableObject {
    @Published var portfolioData: PortfolioSnapshot?
    @Published var investData: AccountData?
    @Published var isaData: AccountData?
    @Published var isLoading = false
    @Published var errorMessage: String?

    // Chart Data
    @Published var pnlHistory: [Double] = [] // Mock data for now, will be real later
    @Published var allocationData: [AllocationItem] = []
    
    private let baseURL = "http://127.0.0.1:8002"
    private var timer: Timer?
    
    init() {
        // Start fetching data
        startLiveSync()
    }
    
    deinit {
        stopLiveSync()
    }
    
    func startLiveSync() {
        // Fetch immediately
        Task { await fetchPortfolioData() }
        
        // Then every 30 seconds
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { [weak self] in
                await self?.fetchPortfolioData()
            }
        }
    }
    
    func stopLiveSync() {
        timer?.invalidate()
        timer = nil
    }
    
    @MainActor
    func fetchPortfolioData() async {
        isLoading = true
        errorMessage = nil

        let url = URL(string: "\(baseURL)/portfolio/live?account_type=all")!
        
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            
            guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            // Try to decode assuming the structure matches what we defined in ChatViewModel
            // We might need to handle the specific response format from the server's /portfolio/live endpoint
            // The server returns the raw result from analyze_portfolio tool, which is a dict.

            // Let's decode it. Note: The server returns JSON.
            let decoder = JSONDecoder()
            let snapshot = try decoder.decode(PortfolioSnapshot.self, from: data)

            withAnimation(.spring(response: 0.6, dampingFraction: 0.8)) {
                self.portfolioData = snapshot
                self.parseAccountData(snapshot: snapshot)
                self.updateChartData(snapshot: snapshot)
                self.isLoading = false
            }
            
        } catch {
            print("Error fetching portfolio: \(error)")
            self.errorMessage = "Failed to sync: \(error.localizedDescription)"
            self.isLoading = false
        }
    }
    
    private func parseAccountData(snapshot: PortfolioSnapshot) {
        // Parse account-specific data from the combined snapshot
        let allPositions = snapshot.positions ?? []

        // Filter positions by account type
        let investPositions = allPositions.filter { $0.accountType == "invest" }
        let isaPositions = allPositions.filter { $0.accountType == "isa" }

        // Try to get account summaries from backend
        let accounts = snapshot.summary?.accounts

        // Parse INVEST account data
        // Always try to create account data if we have summary or positions, or if it's a standard account type
        let investSummary = accounts?["invest"] ?? createAccountSummary(from: investPositions, accountType: "invest", totalSummary: snapshot.summary)
        let investAllocation = calculateAllocationData(for: investPositions)
        self.investData = AccountData(
            summary: investSummary,
            positions: investPositions,
            allocationData: investAllocation
        )

        // Parse ISA account data
        let isaSummary = accounts?["isa"] ?? createAccountSummary(from: isaPositions, accountType: "isa", totalSummary: snapshot.summary)
        let isaAllocation = calculateAllocationData(for: isaPositions)
        self.isaData = AccountData(
            summary: isaSummary,
            positions: isaPositions,
            allocationData: isaAllocation
        )
    }

    private func createAccountSummary(from positions: [Position], accountType: String, totalSummary: PortfolioSummary?) -> AccountSummary {
        // Calculate account summary from positions when backend doesn't provide it
        let totalValue = positions.reduce(0.0) { sum, pos in
            sum + ((pos.currentPrice ?? 0) * (pos.quantity ?? 0))
        }

        let totalInvested = positions.reduce(0.0) { sum, pos in
            sum + ((pos.averagePrice ?? 0) * (pos.quantity ?? 0))
        }

        let totalPnl = positions.reduce(0.0) { sum, pos in
            sum + (pos.ppl ?? 0)
        }

        // Use cash from total summary or default values
        let cashBalance = totalSummary?.cashBalance ?? CashBalance(total: 0.0, free: 0.0)

        return AccountSummary(
            totalInvested: totalInvested,
            currentValue: totalValue,
            totalPnl: totalPnl,
            cashBalance: cashBalance
        )
    }

    private func calculateAllocationData(for positions: [Position]) -> [AllocationItem] {
        // Calculate allocation data for a specific set of positions
        let positionValues = positions.map { pos -> (Position, Double) in
            let price = pos.currentPrice ?? 0.0
            let qty = pos.quantity ?? 0.0
            return (pos, price * qty)
        }

        let sorted = positionValues.sorted { $0.1 > $1.1 }
        let top5 = sorted.prefix(5)

        var allocationData = top5.map { (pos, val) in
            AllocationItem(label: pos.ticker ?? "Unknown", value: val)
        }

        // Add "Others" if needed
        if positionValues.count > 5 {
            let otherVal = sorted.dropFirst(5).reduce(0.0) { result, item in
                result + item.1
            }
            if otherVal > 0 {
                allocationData.append(AllocationItem(label: "Others", value: otherVal))
            }
        }

        return allocationData
    }

    private func updateChartData(snapshot: PortfolioSnapshot) {
        // Update overall allocation data based on all positions
        guard let positions = snapshot.positions else { return }

        // 1. Calculate values for each position first to avoid complex expressions in sort/reduce
        let positionValues = positions.map { pos -> (Position, Double) in
            let price = pos.currentPrice ?? 0.0
            let qty = pos.quantity ?? 0.0
            return (pos, price * qty)
        }

        // 2. Sort by value descending
        let sorted = positionValues.sorted { $0.1 > $1.1 }

        // 3. Take top 5
        let top5 = sorted.prefix(5)

        // 4. Map to AllocationItems
        self.allocationData = top5.map { (pos, val) in
            AllocationItem(label: pos.ticker ?? "Unknown", value: val)
        }

        // 5. Add "Others" if needed
        if positionValues.count > 5 {
            let otherPositions = sorted.dropFirst(5)
            let otherVal = otherPositions.reduce(0.0) { result, item in
                result + item.1
            }

            if otherVal > 0 {
                self.allocationData.append(AllocationItem(label: "Others", value: otherVal))
            }
        }
    }
}

struct AccountData {
    let summary: AccountSummary
    let positions: [Position]
    let allocationData: [AllocationItem]
}

struct AllocationItem: Identifiable {
    let id = UUID()
    let label: String
    let value: Double
}
