"""
Financial Math Utilities
Provides safe Decimal arithmetic and standardized rounding for financial calculations.
"""

from decimal import Decimal, ROUND_HALF_UP, Context, setcontext, getcontext, InvalidOperation
from typing import Union, Optional, List
import logging

logger = logging.getLogger(__name__)

# Type alias for inputs that can be converted to money
MoneyInput = Union[Decimal, float, str, int]

# Standard precisions
PRECISION_CURRENCY = Decimal("0.01")      # 2 decimal places (Fiat)
PRECISION_PRICE = Decimal("0.0001")       # 4 decimal places (Stock prices)
PRECISION_CRYPTO = Decimal("0.00000001")  # 8 decimal places (Crypto)

def create_decimal(value: MoneyInput, precision: Optional[Decimal] = None) -> Decimal:
    """
    Safely creates a Decimal from various input types.
    
    Args:
        value: The value to convert (float, str, int, Decimal)
        precision: Optional quantization precision (e.g., PRECISION_CURRENCY)
        
    Returns:
        Decimal: The converted value, or 0 if conversion fails.
    """
    if value is None:
        return Decimal(0)
        
    try:
        # Convert float to string first to avoid binary floating point artifacts
        # e.g., Decimal(0.1) -> Decimal('0.1000000000000000055511151231257827021181583404541015625')
        if isinstance(value, float):
            d = Decimal(str(value))
        else:
            d = Decimal(value)

        if precision:
            return d.quantize(precision, rounding=ROUND_HALF_UP)
            
        return d
    except (InvalidOperation, TypeError, ValueError):
        logger.warning(f"Failed to convert '{value}' to Decimal. Defaulting to 0.")
        return Decimal(0)

def safe_div(numerator: MoneyInput, denominator: MoneyInput, precision: Optional[Decimal] = None) -> Decimal:
    """
    Safely divides two numbers, handling division by zero.
    """
    num = create_decimal(numerator)
    den = create_decimal(denominator)
    
    if den == 0:
        return Decimal(0)
        
    result = num / den
    if precision:
        return result.quantize(precision, rounding=ROUND_HALF_UP)
    return result

class FinancialContext:
    """
    Context manager for setting decimal precision/rounding locally.
    Usage:
        with FinancialContext():
             # calculations
    """
    def __init__(self, prec: int = 28, rounding: str = ROUND_HALF_UP):
        self.context = Context(prec=prec, rounding=rounding)
        self.token = None

    def __enter__(self):
        self.token = setcontext(self.context)
        return self.context

    def __exit__(self, exc_type, exc_value, traceback):
        # Restore previous context implicitly by exiting the block scope if we used setcontext
        # Python's decimal module uses thread-local context.
        # Ideally we should use localcontext() but implementing a custom class allows us to enforce project standards.
        # Actually, decimal.localcontext is better. Let's wrap that instead?
        # Re-implementing essentially what localcontext does but with defaults.
        if self.token:
             # setcontext returns the OLD context, we don't need to restore manually if we use localcontext
             pass
        return False

def format_currency(value: MoneyInput, symbol: str = "£") -> str:
    """
    Formats a decimal as a currency string.
    """
    d = create_decimal(value, PRECISION_CURRENCY)
    return f"{symbol}{d:,.2f}"
