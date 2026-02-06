from decimal import Decimal
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, ConfigDict

class PriceData(BaseModel):
    """
    Represents a single price point or candle using Decimal for precision.
    """
    ticker: str
    timestamp: str  # ISO 8601
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    
    @field_validator('open', 'high', 'low', 'close', mode='before')
    @classmethod
    def convert_to_decimal(cls, v):
        if v is None:
            return Decimal('0.0')
        if isinstance(v, (float, int, str)):
            return Decimal(str(v))
        return v

class Position(BaseModel):
    """
    Represents a portfolio position with safe monetary types.
    """
    ticker: str
    quantity: Decimal
    average_price: Decimal = Field(..., alias="averagePrice")
    current_price: Decimal = Field(..., alias="currentPrice")
    market_value: Decimal = Field(..., alias="marketValue")
    unrealized_pnl: Decimal = Field(..., alias="unrealizedPnl")
    unrealized_pnl_percent: Decimal = Field(..., alias="unrealizedPnlPercent")
    currency: str = "USD"
    
    # Optional display fields
    current_price_display: Optional[str] = Field(None, alias="currentPriceDisplay")
    average_price_display: Optional[str] = Field(None, alias="averagePriceDisplay")
    
    model_config = ConfigDict(populate_by_name=True)
        
    @field_validator('quantity', 'average_price', 'current_price', 'market_value', 'unrealized_pnl', 'unrealized_pnl_percent', mode='before')
    @classmethod
    def to_decimal(cls, v):
        if v is None:
            return Decimal('0.0')
        return Decimal(str(v))

class PortfolioData(BaseModel):
    """
    Aggregated portfolio view.
    """
    total_value: Decimal
    cash_balance: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_percent: Decimal
    buying_power: Decimal
    positions: List[Position] = []
    
    @field_validator('total_value', 'cash_balance', 'unrealized_pnl', 'unrealized_pnl_percent', 'buying_power', mode='before')
    @classmethod
    def to_decimal(cls, v):
        if v is None:
            return Decimal('0.0')
        return Decimal(str(v))
