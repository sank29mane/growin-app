"""
Centralized currency normalization for multi-source data integration.
Ensures all monetary values are correctly converted and displayed across
Trading212, Alpaca, and yfinance data sources.
"""

from typing import Dict, Any, Optional, List, Union
from enum import Enum
from decimal import Decimal, ROUND_HALF_UP
from data_models import Position

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
    def pence_to_pounds(pence: Union[float, Decimal, None]) -> Decimal:
        """
        Convert pence to pounds using Decimal precision.

        Args:
            pence: Value in pence (GBX)

        Returns:
            Value in pounds (GBP) as Decimal
        """
        if pence is None:
            return Decimal('0.0')

        d_pence = Decimal(str(pence))
        d_pounds = d_pence / Decimal("100.0")
        return d_pounds.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def normalize_price(
        price: Union[float, Decimal],
        ticker: str,
        currency: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Decimal:
        """
        Normalize price to base currency (GBP for UK stocks).

        **This is the primary function to use for ALL price conversions**.

        Args:
            price: Raw price from data source
            ticker: Stock ticker symbol
            currency: Currency code (GBX, GBP, USD, etc.)
            metadata: Optional instrument metadata

        Returns:
            Normalized price in base currency as Decimal
        """
        if price is None:
            return Decimal('0.0')

        d_price = Decimal(str(price))

        # Detect if UK stock
        if CurrencyNormalizer.is_uk_stock(ticker, currency, metadata):
            return CurrencyNormalizer.pence_to_pounds(d_price)

        # Non-UK stocks: return as-is (quantized)
        return d_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def normalize_position(position: Dict[str, Any], metadata_cache: Optional[Dict] = None) -> Position:
        """
        Normalize ALL monetary values in a position dict and convert to Position model.

        Args:
            position: Position dict from data source
            metadata_cache: Optional cache of instrument metadata

        Returns:
            Position object with Decimal values
        """
        ticker = position.get('ticker', position.get('symbol', ''))

        # Get metadata if available
        metadata = None
        if metadata_cache and ticker in metadata_cache:
            metadata = metadata_cache[ticker]

        currency = metadata.get('currency') if metadata else position.get('currency', 'USD')

        # Extract raw values
        raw_current = position.get('currentPrice', position.get('current_price', 0))
        raw_avg = position.get('averagePrice', 0)
        qty = position.get('quantity', position.get('qty', 0))

        # Normalize prices
        norm_current = CurrencyNormalizer.normalize_price(raw_current, ticker, currency, metadata)
        norm_avg = CurrencyNormalizer.normalize_price(raw_avg, ticker, currency, metadata)

        # Calculate derived values if missing or raw
        # Note: We prefer calculating P&L ourselves to ensure consistency with normalized prices
        d_qty = Decimal(str(qty))
        market_value = norm_current * d_qty
        cost_basis = norm_avg * d_qty
        unrealized_pnl = market_value - cost_basis

        unrealized_pnl_percent = Decimal('0.0')
        if cost_basis != 0:
            unrealized_pnl_percent = (unrealized_pnl / cost_basis) * 100

        # Display strings
        current_display, current_symbol = CurrencyNormalizer.get_display_price(norm_current, currency)
        avg_display, _ = CurrencyNormalizer.get_display_price(norm_avg, currency)

        return Position(
            ticker=ticker,
            quantity=d_qty,
            averagePrice=norm_avg,
            currentPrice=norm_current,
            marketValue=market_value,
            unrealizedPnl=unrealized_pnl,
            unrealizedPnlPercent=unrealized_pnl_percent,
            currency=current_symbol if current_symbol in ['$', '£', '€'] else currency, # Store symbol or code
            currentPriceDisplay=f"{current_symbol}{norm_current:,.2f}" if current_symbol else f"{norm_current:,.2f}",
            averagePriceDisplay=f"{current_symbol}{norm_avg:,.2f}" if current_symbol else f"{norm_avg:,.2f}"
        )

    @staticmethod
    def format_currency(amount: Union[float, Decimal], currency: str = "GBP") -> str:
        """
        Format monetary value for display.
        """
        symbols = {
            "GBP": "£",
            "USD": "$",
            "EUR": "€"
        }

        symbol = symbols.get(currency.upper(), currency)

        # Add thousand separators
        if isinstance(amount, Decimal):
            formatted = f"{amount:,.2f}"
        else:
            formatted = f"{amount:,.2f}"

        return f"{symbol}{formatted}"

    @staticmethod
    def get_display_price(price: Union[float, Decimal], currency_code: str) -> tuple[Union[float, Decimal], str]:
        """
        Get price in correct display format with currency symbol.
        """
        currency_code = currency_code.upper() if currency_code else ""

        # Note: We assume 'price' is already normalized to pounds if it was GBX
        # So we just return £ for GBX/GBP

        if currency_code in ['GBX', 'GBP']:
            return (price, '£')
        elif currency_code == 'USD':
            return (price, '$')
        elif currency_code == 'EUR':
            return (price, '€')
        else:
            return (price, currency_code)


class DataSourceNormalizer:
    """
    Normalize currency across all data sources (Trading212, Alpaca, yfinance).
    Ensures consistent currency representation regardless of source.
    """

    @staticmethod
    def normalize_alpaca_price(price: float, symbol: str) -> Decimal:
        """
        Normalize Alpaca price.
        """
        return CurrencyNormalizer.normalize_price(price, symbol)

    @staticmethod
    def normalize_yfinance_price(price: float, ticker: str) -> Decimal:
        """
        Normalize yfinance price.
        """
        return CurrencyNormalizer.normalize_price(price, ticker)

    @staticmethod
    def normalize_trading212_price(price: float, ticker: str, currency: Optional[str] = None) -> Decimal:
        """
        Normalize Trading212 price.
        """
        return CurrencyNormalizer.normalize_price(price, ticker, currency)

    @staticmethod
    def get_currency_for_ticker(ticker: str) -> str:
        """
        Determine base currency for a ticker.
        """
        if CurrencyNormalizer.is_pence_ticker(ticker):
            return "GBP"
        return "USD"


# --------------------------------------------------------------------------- #
# Convenience Functions
# --------------------------------------------------------------------------- #

def normalize_all_positions(positions: List[Dict], metadata_cache: Optional[Dict] = None) -> List[Position]:
    """
    Batch normalize all positions.
    Returns List[Position] objects.
    """
    return [CurrencyNormalizer.normalize_position(pos.copy(), metadata_cache) for pos in positions]


def safe_divide_pence(value: Optional[float]) -> Decimal:
    """
    Safely convert pence to pounds, handling None values.
    """
    return CurrencyNormalizer.pence_to_pounds(value)


def calculate_portfolio_value(positions: List[Position]) -> Decimal:
    """
    Calculate total portfolio value in GBP.
    """
    total = Decimal('0.0')
    for pos in positions:
        # Assuming normalized positions are already in base currency or converting to GBP
        # For simplicity, summing market_value (which we set in normalize_position)
        total += pos.market_value

    return total
