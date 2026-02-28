"""
Market Context Data Structure
Aggregates all data from specialist agents for the Decision Agent.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, ConfigDict, field_serializer
from datetime import datetime, timezone
from enum import Enum
from decimal import Decimal
from utils.financial_math import create_decimal


class Signal(str, Enum):
    """Trading signal types"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NEUTRAL = "NEUTRAL"


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase"""
    return "".join(word.capitalize() if i > 0 else word for i, word in enumerate(string.split("_")))


class TimeSeriesItem(BaseModel):
    """Single data point for a chart"""
    timestamp: int  # ms
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Optional[Decimal] = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ForecastData(BaseModel):
    """TTM forecast predictions"""
    ticker: str
    forecast_24h: Decimal
    forecast_48h: Optional[Decimal] = None
    forecast_7d: Optional[Decimal] = None
    confidence: str = "MEDIUM"  # HIGH, MEDIUM, LOW
    trend: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    algorithm: str = "Unknown" # e.g. "TTM-Zero", "LinearRegression"
    is_fallback: bool = False
    note: Optional[str] = None  # Reason for fallback or extra info
    raw_series: List[TimeSeriesItem] = [] # Full forecast series for charts
    auxiliary_forecasts: Optional[List[Dict[str, Any]]] = None # Secondary model predictions for comparison

    model_config = ConfigDict(populate_by_name=True)


class QuantData(BaseModel):
    """Technical indicators from QuantAgent"""
    ticker: str
    rsi: Optional[Decimal] = None
    macd: Optional[Dict[str, Decimal]] = None
    bollinger_bands: Optional[Dict[str, Decimal]] = None
    signal: Signal = Signal.NEUTRAL
    support_level: Optional[Decimal] = None
    resistance_level: Optional[Decimal] = None

    model_config = ConfigDict(populate_by_name=True)


class PortfolioData(BaseModel):
    """Portfolio holdings and metrics"""
    # Summary (matching frontend PortfolioSummary struct)
    total_positions: int = 0
    total_invested: Decimal = Decimal(0)
    total_value: Decimal = Decimal(0)  # Used as currentValue in frontend
    total_pnl: Decimal = Decimal(0)
    pnl_percent: float = 0.0 # Percentages can remain float
    net_deposits: Decimal = Decimal(0)
    cash_balance: Dict[str, Decimal] = {"total": Decimal(0), "free": Decimal(0)}
    accounts: Optional[Dict[str, Any]] = None
    
    # Detailed data
    positions: List[Dict[str, Any]] = []
    top_holdings: List[str] = []
    portfolio_history: List[Dict[str, Any]] = [] # Historical points for charting/analysis
    
    # Nested summary for frontend compatibility
    @property
    def summary(self) -> Dict[str, Any]:
        return {
            "total_positions": self.total_positions,
            "total_invested": self.total_invested,
            "current_value": self.total_value,
            "total_pnl": self.total_pnl,
            "total_pnl_percent": self.pnl_percent,
            "cash_balance": {
                "total": self.cash_balance.get("total", Decimal(0)),
                "free": self.cash_balance.get("free", Decimal(0))
            },
            "accounts": self.accounts
        }
    
    @property
    def snapshot(self) -> Dict[str, Any]:
        """Wrap summary and positions for Swift PortfolioSnapshot compatibility"""
        return {
            "summary": self.summary,
            "positions": self.positions
        }
    
    def model_dump(self, **kwargs) -> Dict[str, Any]:
        data = super().model_dump(**kwargs)
        # Inject 'summary' and 'snapshot' for frontend compatibility
        data["total_value"] = self.total_value
        data["total_pnl"] = self.total_pnl
        data["pnl_percent"] = self.pnl_percent
        data["cash_balance"] = self.cash_balance
        
        data["summary"] = self.summary
        data["snapshot"] = self.snapshot
        return data

    model_config = ConfigDict(populate_by_name=True)


class NewsArticle(BaseModel):
    """Rich news article data"""
    title: str
    description: Optional[str] = None
    source: str
    url: Optional[str] = None
    sentiment: Optional[Decimal] = None


class ResearchData(BaseModel):
    """News and sentiment analysis"""
    ticker: str
    sentiment_score: Decimal = Decimal(0)  # -1 to 1
    sentiment_label: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    articles: List[NewsArticle] = []
    top_headlines: List[str] = []  # For backward compatibility
    sources: List[str] = []  # For backward compatibility


class SocialData(BaseModel):
    """Social sentiment data structure"""
    ticker: str
    sentiment_score: Decimal = Decimal(0)  # -1 to 1
    sentiment_label: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    mention_volume: str = "LOW" # LOW, MEDIUM, HIGH
    top_discussions: List[str] = []
    platforms: List[str] = []

    model_config = ConfigDict(populate_by_name=True)


class WhaleData(BaseModel):
    """Institutional/Large trade activity"""
    ticker: str
    large_trades: List[Dict[str, Any]] = []
    institutional_holdings: List[Dict[str, Any]] = [] # SOTA 2026: 13F Integration
    unusual_volume: bool = False
    sentiment_impact: str = "NEUTRAL" # BULLISH, BEARISH, NEUTRAL
    summary: str = ""

    model_config = ConfigDict(populate_by_name=True)


class GoalData(BaseModel):
    """Goal-based investment plan data"""
    target_returns_percent: Decimal
    duration_years: Decimal
    initial_capital: Decimal
    risk_profile: str  # LOW, MEDIUM, HIGH
    feasibility_score: Decimal = Decimal(0)  # 0 to 1
    optimal_weights: Dict[str, Decimal] = {}  # ticker: weight
    expected_annual_return: Decimal = Decimal(0)
    expected_volatility: Decimal = Decimal(0)
    sharpe_ratio: Decimal = Decimal(0)
    simulated_final_value_avg: Decimal = Decimal(0)
    probability_of_success: Decimal = Decimal(0)
    suggested_instruments: List[Dict[str, Any]] = []
    simulated_growth_path: List[Dict[str, Any]] = []  # List of {year, value, target}
    rebalancing_strategy: Optional[Dict[str, Any]] = None
    implementation: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(populate_by_name=True)


class PriceData(BaseModel):
    """Current price information"""
    ticker: str
    current_price: Decimal
    currency: str = "USD"
    source: str = "Alpaca"
    variance: Optional[float] = None  # From PriceValidator
    validated: bool = False
    history_series: List[TimeSeriesItem] = [] # Historical series for charts

    model_config = ConfigDict(populate_by_name=True)


from agents.base_agent import TelemetryData


class MarketContext(BaseModel):
    """
    Aggregated market context from all specialist agents.
    This is the comprehensive data structure passed to the Decision Agent.
    """
    
    # Metadata
    query: str
    intent: str = "analytical"
    routing_reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ticker: Optional[str] = None
    
    # Specialist Agent Data
    price: Optional[PriceData] = None
    forecast: Optional[ForecastData] = None
    quant: Optional[QuantData] = None
    portfolio: Optional[PortfolioData] = None
    research: Optional[ResearchData] = None
    social: Optional[SocialData] = None
    whale: Optional[WhaleData] = None
    goal: Optional[GoalData] = None
    
    # Agent Status & Telemetry
    agents_executed: List[str] = []
    agents_failed: List[str] = []
    telemetry_trace: List[TelemetryData] = []
    total_latency_ms: float = 0.0
    
    # Additional Context
    user_context: Dict[str, Any] = {}
    reasoning: Optional[str] = None  # SOTA 2026: Store extracted Chain of Thought (CoT)
    
    def add_agent_result(self, agent_name: str, success: bool, latency_ms: float, telemetry: Optional[TelemetryData] = None):
        """Track which agents ran and their status with full telemetry"""
        if success:
            self.agents_executed.append(agent_name)
        else:
            self.agents_failed.append(agent_name)
        
        if telemetry:
            self.telemetry_trace.append(telemetry)
            
        self.total_latency_ms += latency_ms
    
    def is_complete(self) -> bool:
        """Check if we have minimum required data"""
        # Context is complete if we have at least one data source
        return (
            self.price is not None or
            self.portfolio is not None or
            self.quant is not None or
            self.forecast is not None
        )
    
    def get_summary(self) -> str:
        """Generate human-readable summary of available data"""
        parts = []
        
        if self.price:
            parts.append(f"Price: {self.price.currency}{self.price.current_price:.2f}")
        
        if self.forecast:
            parts.append(f"Forecast 24h: ${self.forecast.forecast_24h:.2f} ({self.forecast.confidence})")
        
        if self.quant:
            parts.append(f"Technical Signal: {self.quant.signal}")
        
        if self.research:
            parts.append(f"Sentiment: {self.research.sentiment_label}")
        
        if self.portfolio:
            parts.append(f"Portfolio Value: £{self.portfolio.total_value:.2f}")

        if self.whale:
            parts.append(f"Whales: {self.whale.sentiment_impact}")
        
        return " | ".join(parts) if parts else "No data available"
    
    model_config = ConfigDict(populate_by_name=True)

    @field_serializer('timestamp')
    def serialize_dt(self, dt: datetime, _info):
        return dt.isoformat()

    @field_serializer('price', 'forecast', 'quant', 'portfolio', 'research', 'social', 'whale', 'goal', check_fields=False)
    def serialize_nested(self, v, _info):
        if v is None:
            return None
        return v.model_dump(mode='json')
