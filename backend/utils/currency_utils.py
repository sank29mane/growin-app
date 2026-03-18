"""
Centralized currency normalization for multi-source data integration.
Ensures all monetary values are correctly converted and displayed across
Trading212, Alpaca, and yfinance data sources.
"""

from typing import Dict, Any, Optional, List, Union
from enum import Enum
from decimal import Decimal, ROUND_HALF_UP

class Currency(str, Enum):
    """Supported currencies"""
    GBP = "GBP"  # British Pound (base unit)
    GBX = "GBX"  # British Pence (1/100 of GBP)
    USD = "USD"  # US Dollar
    EUR = "EUR"  # Euro

class CurrencyNormalizer:
    """
    Centralized currency conversion and normalization.

    **Default Behavior**: All UK stocks (GBX) -> GBP (pounds)
    **Display Format**: Always show base currency (£ for GBP, $ for USD)
    """

    # Exchange symbols that definitively indicate pence if no other currency info
    PENCE_EXCHANGES = {".L", ".IL"}  # London Stock Exchange, Irish Stock Exchange

    @staticmethod
    def is_pence_ticker(ticker: str) -> bool:
        """
        Determine if ticker is in pence based on exchange suffix.
        """
        if not ticker:
            return False
        ticker_upper = ticker.upper()
        return any(ticker_upper.endswith(suffix) for suffix in CurrencyNormalizer.PENCE_EXCHANGES)

    @staticmethod
    def pence_to_pounds(pence: Union[float, Decimal, str, None]) -> Decimal:
        """
        Convert pence to pounds using Decimal precision.
        """
        if pence is None:
            return Decimal("0.00")
        try:
            d_pence = Decimal(str(pence))
            d_pounds = d_pence / Decimal("100.0")
            return d_pounds.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()
        except (ValueError, TypeError, ArithmeticError):
            return Decimal("0.00")

    @staticmethod
    def normalize_price(
        price: Union[float, Decimal, str, None],
        ticker: str,
        currency: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> Decimal:
        """
        Normalize price to base currency (GBP for UK stocks).
        """
        if price is None or price == "":
            return Decimal("0.0")
        try:
            d_price = Decimal(str(price))
        except (ValueError, TypeError, ArithmeticError):
            return Decimal("0.0")

        # Determine if it should be treated as pence
        is_pence = False
        
        # Priority 1: Explicit currency code from source
        if currency:
            curr_up = currency.upper()
            if curr_up == "GBX" or currency == "GBp":
                is_pence = True
            elif curr_up == "GBP":
                # If ticker is .L, we usually treat it as pence.
                if CurrencyNormalizer.is_pence_ticker(ticker):
                    is_pence = True
        
        # Priority 2: Metadata
        elif metadata and metadata.get("currency"):
            meta_curr = metadata.get("currency")
            meta_up = meta_curr.upper()
            if meta_up == "GBX" or meta_curr == "GBp":
                is_pence = True
            elif meta_up == "GBP" and CurrencyNormalizer.is_pence_ticker(ticker):
                is_pence = True

        # Priority 3: Ticker suffix (fallback)
        elif CurrencyNormalizer.is_pence_ticker(ticker):
            # But if currency is explicitly USD, dont convert
            if currency and currency.upper() == "USD":
                is_pence = False
            else:
                is_pence = True
            
        if is_pence:
            return CurrencyNormalizer.pence_to_pounds(d_price)

        return d_price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP).normalize()

    @staticmethod
    def format_currency(amount: Union[float, Decimal], currency: str = "GBP") -> str:
        """
        Format monetary value for display.
        """
        symbols = {"GBP": "£", "GBX": "£", "GBp": "£", "USD": "$", "EUR": "€"}
        symbol = symbols.get(currency.upper(), symbols.get(currency, currency))
        try:
            d_amount = Decimal(str(amount))
            return f"{symbol}{d_amount:,.2f}"
        except (ValueError, TypeError, ArithmeticError):
            return f"{symbol}0.00"

    @staticmethod
    def get_display_price(price: Union[float, Decimal], currency_code: str) -> tuple[Decimal, str]:
        """
        Get price in correct display format with currency symbol.
        """
        currency_code = currency_code or "USD"
        try:
            d_price = Decimal(str(price))
        except (ValueError, TypeError, ArithmeticError):
            d_price = Decimal("0.00")
        if currency_code.upper() in ["GBX", "GBP", "GBp"]:
            return (d_price, "£")
        elif currency_code.upper() == "USD":
            return (d_price, "$")
        elif currency_code.upper() == "EUR":
            return (d_price, "€")
        else:
            return (d_price, currency_code)
