from decimal import Decimal
from typing import Optional, List, Dict, Union
from datetime import date, datetime
from enum import Enum
from pydantic import BaseModel, Field, field_validator, ConfigDict

class AssetType(str, Enum):
    EQUITY = "EQUITY"
    OPTION = "OPTION"
    FX = "FX"
    CRYPTO = "CRYPTO"

class OptionGreeks(BaseModel):
    delta: Optional[Decimal] = None
    gamma: Optional[Decimal] = None
    theta: Optional[Decimal] = None
    vega: Optional[Decimal] = None
    rho: Optional[Decimal] = None

class OptionData(BaseModel):
    underlying_ticker: str
    strike_price: Decimal
    expiration_date: date
    option_type: str # 'call' or 'put'
    contract_size: int = 100
    open_interest: Optional[int] = None
    greeks: Optional[OptionGreeks] = None

class FXData(BaseModel):
    base_currency: str
    quote_currency: str
    lot_size: int = 100000

class CryptoData(BaseModel):
    base_asset: str
    quote_asset: str = "USD"
    blockchain: Optional[str] = None

class PriceData(BaseModel):
    """
    Represents a single price point or candle using Decimal for precision.
    Supports Multi-Asset types.
    """
    ticker: str
    asset_type: AssetType = AssetType.EQUITY
    timestamp: str  # ISO 8601
    t: Optional[int] = None # Unix timestamp in milliseconds
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal('0')
    
    # Metadata for specific asset types
    option_details: Optional[OptionData] = None
    fx_details: Optional[FXData] = None
    crypto_details: Optional[CryptoData] = None
    
    @field_validator('open', 'high', 'low', 'close', 'volume', mode='before')
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
    Supports Multi-Asset types.
    """
    ticker: str
    asset_type: AssetType = AssetType.EQUITY
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
    
    # Metadata for specific asset types
    option_details: Optional[OptionData] = None
    
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

class DividendData(BaseModel):
    """
    Unified dividend record supporting Trading 212 and Alpaca formats.
    """
    ticker: str
    amount: Decimal
    ex_date: date = Field(..., alias="ex_date") # Standard for both internally
    payment_date: Optional[date] = Field(None, alias="payment_date")
    record_date: Optional[date] = Field(None, alias="record_date")
    status: str = "CONFIRMED"
    frequency: str = "QUARTERLY"
    currency: str = "USD"
    
    # AI/ML Preprocessed Metrics
    iqr_scaled_amount: Optional[float] = None
    sempo_filtered_signal: Optional[float] = None
    
    model_config = ConfigDict(populate_by_name=True)

    @field_validator('amount', mode='before')
    @classmethod
    def to_decimal(cls, v):
        if v is None:
            return Decimal('0.0')
        if isinstance(v, (float, int, str)):
            return Decimal(str(v))
        return v
