import Foundation
import Combine
import SwiftUI

@Observable @MainActor
class DashboardViewModel {
    var portfolioData: PortfolioSnapshot?
    var investData: AccountData?
    var isaData: AccountData?
    var isLoading = false
    var errorMessage: String?

    // Chart Data
    var pnlHistory: [Double] = [] 
    var allocationData: [GrowinAllocationData] = []
    
    private let dataService = PortfolioDataService()
    private var syncTask: Task<Void, Never>?
    
    init() {
        startLiveSync()
    }
    
    func startLiveSync() {
        syncTask?.cancel()
        syncTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self = self else { return }
                await self.fetchPortfolioData()
                try? await Task.sleep(for: .seconds(30))
            }
        }
    }
    
    func stopLiveSync() {
        syncTask?.cancel()
        syncTask = nil
    }
    
    func fetchPortfolioData() async {
        isLoading = true
        errorMessage = nil

        do {
            let snapshot = try await dataService.fetchPortfolio(accountType: "all")

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
        let allPositions = snapshot.positions ?? []
        let investPositions = allPositions.filter { $0.accountType == "invest" }
        let isaPositions = allPositions.filter { $0.accountType == "isa" }
        let accounts = snapshot.summary?.accounts

        let investSummary = accounts?["invest"] ?? createAccountSummary(from: investPositions, accountType: "invest", totalSummary: snapshot.summary)
        let investAllocation = calculateAllocationData(for: investPositions)
        self.investData = AccountData(
            summary: investSummary,
            positions: investPositions,
            allocationData: investAllocation
        )

        let isaSummary = accounts?["isa"] ?? createAccountSummary(from: isaPositions, accountType: "isa", totalSummary: snapshot.summary)
        let isaAllocation = calculateAllocationData(for: isaPositions)
        self.isaData = AccountData(
            summary: isaSummary,
            positions: isaPositions,
            allocationData: isaAllocation
        )
    }

    private func createAccountSummary(from positions: [Position], accountType: String, totalSummary: PortfolioSummary?) -> AccountSummary {
        let totalValue = positions.reduce(Decimal(0)) { sum, pos in
            sum + ((pos.currentPrice ?? Decimal(0)) * (pos.quantity ?? Decimal(0)))
        }

        let totalInvested = positions.reduce(Decimal(0)) { sum, pos in
            sum + ((pos.averagePrice ?? Decimal(0)) * (pos.quantity ?? Decimal(0)))
        }

        let totalPnl = positions.reduce(Decimal(0)) { sum, pos in
            sum + (pos.ppl ?? Decimal(0))
        }

        let cashBalance = totalSummary?.cashBalance ?? CashBalance(total: Decimal(0), free: Decimal(0))

        return AccountSummary(
            totalInvested: totalInvested,
            currentValue: totalValue,
            totalPnl: totalPnl,
            cashBalance: cashBalance
        )
    }

    private func calculateAllocationData(for positions: [Position]) -> [GrowinAllocationData] {
        let positionValues = positions.map { pos -> (Position, Decimal) in
            let price = pos.currentPrice ?? Decimal(0)
            let qty = pos.quantity ?? Decimal(0)
            return (pos, price * qty)
        }

        let sorted = positionValues.sorted { $0.1 > $1.1 }
        let top5 = sorted.prefix(5)

        var allocationData = top5.map { (pos, val) in
            GrowinAllocationData(label: pos.ticker ?? "Unknown", value: val)
        }

        if positionValues.count > 5 {
            let otherVal = sorted.dropFirst(5).reduce(Decimal(0)) { result, item in
                result + item.1
            }
            if otherVal > 0 {
                allocationData.append(GrowinAllocationData(label: "Others", value: otherVal))
            }
        }

        return allocationData
    }

    private func updateChartData(snapshot: PortfolioSnapshot) {
        guard let positions = snapshot.positions else { return }
        self.allocationData = calculateAllocationData(for: positions)
    }
}

struct AccountData {
    let summary: AccountSummary
    let positions: [Position]
    let allocationData: [GrowinAllocationData]
}
