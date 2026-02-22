import Foundation

// MARK: - Core Model Concurrency
// These models are pure Sendable data containers to be used across actors.

// MARK: - Portfolio Models

struct PortfolioSnapshot: Codable, Sendable {
    let summary: PortfolioSummary?
    let positions: [Position]?
}

struct PortfolioSummary: Codable, Sendable {
    let totalPositions: Int?
    let totalInvested: Double?
    let currentValue: Double?
    let totalPnl: Double?
    let totalPnlPercent: Double?
    let cashBalance: CashBalance?
    let accounts: [String: AccountSummary]?
    
    enum CodingKeys: String, CodingKey {
        case totalPositions = "total_positions"
        case totalInvested = "total_invested"
        case currentValue = "current_value"
        case totalPnl = "total_pnl"
        case totalPnlPercent = "total_pnl_percent"
        case cashBalance = "cash_balance"
        case accounts
    }
}

struct AccountSummary: Codable, Sendable {
    let totalInvested: Double?
    let currentValue: Double?
    let totalPnl: Double?
    let cashBalance: CashBalance?
    
    enum CodingKeys: String, CodingKey {
        case totalInvested = "total_invested"
        case currentValue = "current_value"
        case totalPnl = "total_pnl"
        case cashBalance = "cash_balance"
    }
}

struct CashBalance: Codable, Sendable {
    let total: Double?
    let free: Double?
}

struct Position: Codable, Identifiable, Sendable {
    var id: String { 
        let t = ticker ?? UUID().uuidString
        let a = accountType ?? ""
        return "\(t)-\(a)"
    }
    let ticker: String?
    let name: String?
    let quantity: Double?
    let currentPrice: Double?
    let averagePrice: Double?
    let ppl: Double?
    let accountType: String?
    
    enum CodingKeys: String, CodingKey {
        case ticker, name, quantity
        case currentPrice = "currentPrice"
        case averagePrice = "averagePrice"
        case ppl
        case accountType = "account_type"
    }
}

struct PortfolioHistoryPoint: Codable, Identifiable, Sendable {
    var id: String { timestamp }
    let timestamp: String
    let totalValue: Double
    let totalPnl: Double
    let cashBalance: Double
    
    enum CodingKeys: String, CodingKey {
        case timestamp
        case totalValue = "total_value"
        case totalPnl = "total_pnl"
        case cashBalance = "cash_balance"
    }
    
    var date: Date {
        DateUtils.parse(timestamp)
    }
}

struct TradingConfig: Codable, Sendable {
    let accountType: String
    let investKey: String
    let investSecret: String
    let isaKey: String
    let isaSecret: String
    
    enum CodingKeys: String, CodingKey {
        case accountType = "account_type"
        case investKey = "invest_key"
        case investSecret = "invest_secret"
        case isaKey = "isa_key"
        case isaSecret = "isa_secret"
    }
}

// MARK: - Chart Models

struct ChartResponse: Codable, Sendable {
    let data: [ChartDataPoint]
    let metadata: ChartMetadata
    let error: ChartErrorInfo?
}

struct ChartMetadata: Codable, Sendable {
    let market: String
    let currency: String
    let symbol: String
    let ticker: String
    let provider: String
}

struct ChartErrorInfo: Codable, Sendable {
    let code: String
    let message: String
    let providerErrors: [String]
    let fallbackUsed: Bool
    
    enum CodingKeys: String, CodingKey {
        case code, message
        case providerErrors = "provider_errors"
        case fallbackUsed = "fallback_used"
    }
}

struct ChartDataPoint: Codable, Identifiable, Equatable, Sendable {
    var id: String { timestamp }
    let timestamp: String
    let close: Double
    let high: Double?
    let low: Double?
    let open: Double?
    let volume: Int?
    
    var date: Date {
        DateUtils.parse(timestamp)
    }
}

struct AnalysisResponse: Codable, Sendable {
    let aiAnalysis: String
    let algoSignals: String
    let lastUpdated: String?
    
    enum CodingKeys: String, CodingKey {
        case aiAnalysis = "ai_analysis"
        case algoSignals = "algo_signals"
        case lastUpdated = "last_updated"
    }
}

// MARK: - App Models

enum TimeRange: String, Codable, CaseIterable, Sendable {
    case day, week, month, threeMonths, year, all
    
    var days: Int {
        switch self {
        case .day: return 1
        case .week: return 7
        case .month: return 30
        case .threeMonths: return 90
        case .year: return 365
        case .all: return 3650
        }
    }
}

struct TimeSeriesItem: Codable, Identifiable, Equatable, Sendable {
    var id: Double { timestamp }
    let timestamp: Double
    let open: Double
    let high: Double
    let low: Double
    let close: Double
    let volume: Double?
    
    var date: Date {
        Date(timeIntervalSince1970: timestamp / 1000.0)
    }
}
