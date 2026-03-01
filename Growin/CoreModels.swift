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
    let totalInvested: Decimal?
    let currentValue: Decimal?
    let totalPnl: Decimal?
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
    let totalInvested: Decimal?
    let currentValue: Decimal?
    let totalPnl: Decimal?
    let cashBalance: CashBalance?
    
    enum CodingKeys: String, CodingKey {
        case totalInvested = "total_invested"
        case currentValue = "current_value"
        case totalPnl = "total_pnl"
        case cashBalance = "cash_balance"
    }
}

struct CashBalance: Codable, Sendable {
    let total: Decimal?
    let free: Decimal?
}

struct Position: Codable, Identifiable, Sendable {
    var id: String { 
        if let ticker = ticker, let accountType = accountType {
            return "\(ticker)-\(accountType)"
        }
        return ticker ?? accountType ?? UUID().uuidString
    }
    let ticker: String?
    let name: String?
    let quantity: Decimal?
    let currentPrice: Decimal?
    let averagePrice: Decimal?
    let ppl: Decimal?
    let fxPpl: Decimal?
    let accountType: String?
    
    enum CodingKeys: String, CodingKey {
        case ticker, name, quantity
        case currentPrice = "current_price"
        case averagePrice = "average_price"
        case ppl
        case fxPpl = "fx_ppl"
        case accountType = "account_type"
    }
}

struct PortfolioHistoryPoint: Codable, Identifiable, Sendable {
    var id: String { timestamp }
    let timestamp: String
    let totalValue: Decimal
    let totalPnl: Decimal
    let cashBalance: Decimal
    
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
    let close: Decimal
    let high: Decimal?
    let low: Decimal?
    let open: Decimal?
    let volume: Int?
    let date: Date
    
    init(timestamp: String, close: Decimal, high: Decimal? = nil, low: Decimal? = nil, open: Decimal? = nil, volume: Int? = nil) {
        self.timestamp = timestamp
        self.close = close
        self.high = high
        self.low = low
        self.open = open
        self.volume = volume
        self.date = DateUtils.parse(timestamp)
    }
    
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.timestamp = try container.decode(String.self, forKey: .timestamp)
        self.close = try container.decode(Decimal.self, forKey: .close)
        self.high = try container.decodeIfPresent(Decimal.self, forKey: .high)
        self.low = try container.decodeIfPresent(Decimal.self, forKey: .low)
        self.open = try container.decodeIfPresent(Decimal.self, forKey: .open)
        self.volume = try container.decodeIfPresent(Int.self, forKey: .volume)
        self.date = DateUtils.parse(self.timestamp)
    }
    
    enum CodingKeys: String, CodingKey {
        case timestamp, close, high, low, open, volume
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
    let open: Decimal
    let high: Decimal
    let low: Decimal
    let close: Decimal
    let volume: Decimal?
    
    var date: Date {
        Date(timeIntervalSince1970: timestamp / 1000.0)
    }
}

// MARK: - UI Models

struct GrowinAllocationData: Identifiable, Sendable, Equatable {
    let id: UUID
    let label: String
    let value: Decimal
    
    var doubleValue: Double {
        NSDecimalNumber(decimal: value).doubleValue
    }
    
    init(id: UUID = UUID(), label: String, value: Decimal) {
        self.id = id
        self.label = label
        self.value = value
    }
}

// MARK: - SOTA AI Models

struct ReasoningStep: Codable, Sendable, Identifiable {
    var id: Double { timestamp }
    let agent: String
    let action: String
    let content: String?
    let timestamp: Double
}

struct AgentEvent: Codable, Sendable {
    let eventType: String
    let agent: String
    let status: String
    let step: ReasoningStep?
    let timestamp: Double
    
    enum CodingKeys: String, CodingKey {
        case eventType = "event_type"
        case agent, status, step, timestamp
    }
}

struct AIStrategy: Codable, Sendable, Identifiable {
    var id: String { strategyId }
    let strategyId: String
    let title: String
    let summary: String
    let confidence: Double
    let reasoningTrace: [ReasoningStep]
    let instruments: [InstrumentWeightMapping]
    let riskAssessment: String
    let lastUpdated: Double
    
    enum CodingKeys: String, CodingKey {
        case strategyId = "strategy_id"
        case title, summary, confidence
        case reasoningTrace = "reasoning_trace"
        case instruments
        case riskAssessment = "risk_assessment"
        case lastUpdated = "last_updated"
    }
}

struct InstrumentWeightMapping: Codable, Sendable {
    let ticker: String
    let weight: Double
}
