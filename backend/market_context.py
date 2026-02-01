"""
Market Context Data Structure
Aggregates all data from specialist agents for the Decision Agent.
"""

from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


class Signal(str, Enum):
    """Trading signal types"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NEUTRAL = "NEUTRAL"


class TimeSeriesItem(BaseModel):
    """Single data point for a chart"""
    timestamp: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


class ForecastData(BaseModel):
    """TTM forecast predictions"""
    ticker: str
    forecast_24h: float
    forecast_48h: Optional[float] = None
    forecast_7d: Optional[float] = None
    confidence: str = "MEDIUM"  # HIGH, MEDIUM, LOW
    trend: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    algorithm: str = "Unknown" # e.g. "TTM-Zero", "LinearRegression"
    is_fallback: bool = False
    note: Optional[str] = None  # Reason for fallback or extra info
    raw_series: List[TimeSeriesItem] = [] # Full forecast series for charts
    auxiliary_forecasts: Optional[List[Dict[str, Any]]] = None # Secondary model predictions for comparison


class QuantData(BaseModel):
    """Technical indicators from QuantAgent"""
    ticker: str
    rsi: Optional[float] = None
    macd: Optional[Dict[str, float]] = None
    bollinger_bands: Optional[Dict[str, float]] = None
    signal: Signal = Signal.NEUTRAL
    support_level: Optional[float] = None
    resistance_level: Optional[float] = None


class PortfolioData(BaseModel):
    """Portfolio holdings and metrics"""
    # Summary (matching frontend PortfolioSummary struct)
    total_positions: int = 0
    total_invested: float = 0.0
    total_value: float = 0.0  # Used as currentValue in frontend
    total_pnl: float = 0.0
    pnl_percent: float = 0.0
    net_deposits: float = 0.0
    cash_balance: Dict[str, float] = {"total": 0.0, "free": 0.0}
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
            "cash_balance": self.cash_balance,
            "accounts": self.accounts
        }
    
    def model_dump(self, **kwargs) -> Dict[str, Any]:
        data = super().model_dump(**kwargs)
        # Inject 'summary' for frontend compatibility
        data["summary"] = self.summary
        return data


class NewsArticle(BaseModel):
    """Rich news article data"""
    title: str
    description: Optional[str] = None
    source: str
    url: Optional[str] = None
    sentiment: Optional[float] = None


class ResearchData(BaseModel):
    """News and sentiment analysis"""
    ticker: str
    sentiment_score: float = 0.0  # -1 to 1
    sentiment_label: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    articles: List[NewsArticle] = []
    top_headlines: List[str] = []  # For backward compatibility
    sources: List[str] = []  # For backward compatibility


class SocialData(BaseModel):
    """Social sentiment data structure"""
    ticker: str
    sentiment_score: float = 0.0  # -1 to 1
    sentiment_label: str = "NEUTRAL"
    mention_volume: str = "LOW" # LOW, MEDIUM, HIGH
    top_discussions: List[str] = []
    platforms: List[str] = []


class WhaleData(BaseModel):
    """Institutional/Large trade activity"""
    ticker: str
    large_trades: List[Dict[str, Any]] = []
    unusual_volume: bool = False
    sentiment_impact: str = "NEUTRAL" # BULLISH, BEARISH, NEUTRAL
    summary: str = ""


class GoalData(BaseModel):
    """Goal-based investment plan data"""
    target_returns_percent: float
    duration_years: float
    initial_capital: float
    risk_profile: str  # LOW, MEDIUM, HIGH
    feasibility_score: float = 0.0  # 0 to 1
    optimal_weights: Dict[str, float] = {}  # ticker: weight
    expected_annual_return: float = 0.0
    expected_volatility: float = 0.0
    sharpe_ratio: float = 0.0
    simulated_final_value_avg: float = 0.0
    probability_of_success: float = 0.0
    suggested_instruments: List[Dict[str, Any]] = []
    simulated_growth_path: List[Dict[str, Any]] = []  # List of {year, value, target}
    rebalancing_strategy: Optional[Dict[str, Any]] = None
    implementation: Optional[Dict[str, Any]] = None


class PriceData(BaseModel):
    """Current price information"""
    ticker: str
    current_price: float
    currency: str = "USD"
    source: str = "Alpaca"
    variance: Optional[float] = None  # From PriceValidator
    validated: bool = False
    history_series: List[TimeSeriesItem] = [] # Historical series for charts


class MarketContext(BaseModel):
    """
    Aggregated market context from all specialist agents.
    This is the comprehensive data structure passed to the Decision Agent.
    """
    
    # Metadata
    query: str
    intent: str = "analytical"
    routing_reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
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
    
    # Agent Status
    agents_executed: List[str] = []
    agents_failed: List[str] = []
    total_latency_ms: float = 0.0
    
    # Additional Context
    user_context: Dict[str, Any] = {}
    
    def add_agent_result(self, agent_name: str, success: bool, latency_ms: float):
        """Track which agents ran and their status"""
        if success:
            self.agents_executed.append(agent_name)
        else:
            self.agents_failed.append(agent_name)
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
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
