"""
Centralized currency normalization for multi-source data integration.
Ensures all monetary values are correctly converted and displayed across
Trading212, Alpaca, and yfinance data sources.
"""

from typing import Dict, Any, Optional, List
from enum import Enum

class Currency(str, Enum):
    """Supported currencies"""
    GBP = "GBP"  # British Pound (base unit)
    GBX = "GBX"  # British Pence (1/100 of GBP)
    USD = "USD"  # US Dollar
    EUR = "EUR"  # Euro

class CurrencyNormalizer:
    """
    Centralized currency conversion and normalization.
    
    **Default Behavior**: All UK stocks (GBX) → GBP (pounds)
    **Display Format**: Always show base currency (£ for GBP, $ for USD)
    """
    
    # Exchange symbols that definitively indicate pence
    PENCE_EXCHANGES = {'.L', '.IL'}  # London Stock Exchange, Irish Stock Exchange
    
    @staticmethod
    def is_pence_ticker(ticker: str) -> bool:
        """
        Determine if ticker is in pence based on exchange suffix.
        
        Examples:
            SSLN_EQ.L → True (London Stock Exchange)
            VOD.L → True
            AAPL → False (US stock)
        """
        ticker_upper = ticker.upper()
        return any(ticker_upper.endswith(suffix) for suffix in CurrencyNormalizer.PENCE_EXCHANGES)
    
    @staticmethod
    def is_uk_stock(ticker: str, currency: Optional[str] = None, metadata: Optional[Dict] = None) -> bool:
        """
        Comprehensive UK stock detection.
        
        Priority:
        1. Exchange suffix (.L, .IL)
        2. Currency code (GBX, GBp)
        3. Metadata check (exchange, country)
        """
        # 1. Check ticker suffix (most reliable)
        if CurrencyNormalizer.is_pence_ticker(ticker):
            return True
        
        # 2. Check currency code
        if currency and currency.upper() in ['GBX', 'GBP']:
            # If explicitly GBX, definitely UK
            if currency.upper() == 'GBX':
                return True
            # If GBP, check other signals
            if metadata:
                exchange = metadata.get('exchange', '').upper()
                if 'LONDON' in exchange or 'LSE' in exchange:
                    return True
        
        return False
    
    @staticmethod
    def pence_to_pounds(pence: float) -> float:
        """
        Convert pence to pounds.
        
        Args:
            pence: Value in pence (GBX)
        
        Returns:
            Value in pounds (GBP)
        
        Examples:
            94725 pence → 947.25 pounds
            442.82 pence → 4.43 pounds
        """
        return round(pence / 100.0, 2)
    
    @staticmethod
    def normalize_price(
        price: float,
        ticker: str,
        currency: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> float:
        """
        Normalize price to base currency (GBP for UK stocks).
        
        **This is the primary function to use for ALL price conversions**.
        
        Args:
            price: Raw price from data source
            ticker: Stock ticker symbol
            currency: Currency code (GBX, GBP, USD, etc.)
            metadata: Optional instrument metadata
        
        Returns:
            Normalized price in base currency
        
        Examples:
            normalize_price(94725, "SSLN_EQ.L", "GBX") → 947.25
            normalize_price(150.50, "AAPL", "USD") → 150.50
        """
        # Detect if UK stock
        if CurrencyNormalizer.is_uk_stock(ticker, currency, metadata):
            return CurrencyNormalizer.pence_to_pounds(price)
        
        # Non-UK stocks: return as-is
        return round(price, 2)
    
    @staticmethod
    def normalize_position(position: Dict[str, Any], metadata_cache: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Normalize ALL monetary values in a position dict.
        
        Modifies:
        - currentPrice
        - averagePrice
        - (ppl is already in account currency, leave untouched)
        
        Args:
            position: Position dict from data source
            metadata_cache: Optional cache of instrument metadata
        
        Returns:
            Normalized position dict (modified in-place)
        """
        ticker = position.get('ticker', '')
        
        # Get metadata if available
        metadata = None
        if metadata_cache and ticker in metadata_cache:
            metadata = metadata_cache[ticker]
        
        currency = metadata.get('currency') if metadata else None
        
        # Normalize prices
        if 'currentPrice' in position:
            position['currentPrice'] = CurrencyNormalizer.normalize_price(
                position['currentPrice'],
                ticker,
                currency,
                metadata
            )
        
        if 'averagePrice' in position:
            position['averagePrice'] = CurrencyNormalizer.normalize_price(
                position['averagePrice'],
                ticker,
                currency,
                metadata
            )
        
        return position
    
    @staticmethod
    def format_currency(amount: float, currency: str = "GBP") -> str:
        """
        Format monetary value for display.
        
        Args:
            amount: Monetary value
            currency: Currency code
        
        Returns:
            Formatted string with symbol
        
        Examples:
            format_currency(947.25, "GBP") → "£947.25"
            format_currency(1500.50, "USD") → "$1,500.50"
        """
        symbols = {
            "GBP": "£",
            "USD": "$",
            "EUR": "€"
        }
        
        symbol = symbols.get(currency.upper(), currency)
        
        # Add thousand separators
        formatted = f"{amount:,.2f}"
        
        return f"{symbol}{formatted}"


class DataSourceNormalizer:
    """
    Normalize currency across all data sources (Trading212, Alpaca, yfinance).
    Ensures consistent currency representation regardless of source.
    """
    
    @staticmethod
    def normalize_alpaca_price(price: float, symbol: str) -> float:
        """
        Normalize Alpaca price.
        Alpaca returns USD for US stocks, no conversion needed typically.
        
        Args:
            price: Raw price from Alpaca
            symbol: Stock symbol
        
        Returns:
            Normalized price
        """
        # Alpaca is primarily USD market
        # If we ever support UK stocks via Alpaca, handle here
        return round(price, 2)
    
    @staticmethod
    def normalize_yfinance_price(price: float, ticker: str) -> float:
        """
        Normalize yfinance price.
        yfinance returns pence for LSE stocks (.L suffix).
        
        Args:
            price: Raw price from yfinance
            ticker: Yahoo Finance ticker (e.g., "SSLN.L")
        
        Returns:
            Normalized price in GBP
        
        Examples:
            normalize_yfinance_price(6315, "SSLN.L") → 63.15 GBP
            normalize_yfinance_price(150.50, "AAPL") → 150.50 USD
        """
        # Use existing CurrencyNormalizer logic
        return CurrencyNormalizer.normalize_price(price, ticker)
    
    @staticmethod
    def normalize_trading212_price(price: float, ticker: str, currency: Optional[str] = None) -> float:
        """
        Normalize Trading212 price.
        Trading212 returns GBX (pence) for UK stocks.
        
        Args:
            price: Raw price from Trading212
            ticker: Trading212 ticker format (e.g., "SSLN_EQ.L")
            currency: Currency code from Trading212
        
        Returns:
            Normalized price in GBP
        """
        return CurrencyNormalizer.normalize_price(price, ticker, currency)
    
    @staticmethod
    def get_currency_for_ticker(ticker: str) -> str:
        """
        Determine base currency for a ticker.
        
        Returns:
            Currency code (GBP, USD, EUR, etc.)
        """
        if CurrencyNormalizer.is_pence_ticker(ticker):
            return "GBP"
        # Default to USD for US markets
        return "USD"


# --------------------------------------------------------------------------- #
# Convenience Functions
# --------------------------------------------------------------------------- #

def normalize_all_positions(positions: List[Dict], metadata_cache: Optional[Dict] = None) -> List[Dict]:
    """
    Batch normalize all positions.
    
    Usage:
        positions = await client.get_all_positions()
        normalized = normalize_all_positions(positions, metadata)
    """
    return [CurrencyNormalizer.normalize_position(pos.copy(), metadata_cache) for pos in positions]


def safe_divide_pence(value: Optional[float]) -> float:
    """
    Safely convert pence to pounds, handling None values.
    
    Args:
        value: Value in pence (or None)
    
    Returns:
        Value in pounds (0.0 if None)
    """
    if value is None:
        return 0.0
    return CurrencyNormalizer.pence_to_pounds(value)
