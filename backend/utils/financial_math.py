"""
Financial Math Utilities - Precision-safe calculations for Growin App
Uses Python's decimal module to avoid floating point errors.
Standard: 2026 SOTA Financial Best Practices
"""

from decimal import Decimal, getcontext, ROUND_HALF_UP
from typing import Union, Any

# Standard Financial Precision (4 decimal places for intermediate, 2 for display)
PRECISION_INTERNAL = 4
PRECISION_DISPLAY = 2
PRECISION_CURRENCY = Decimal('0.01')

# Set global context for financial calculations
getcontext().rounding = ROUND_HALF_UP

def create_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal, handling strings, floats, ints, and NaN."""
    if value is None:
        return Decimal('0')
    if isinstance(value, float):
        import math
        if math.isnan(value) or math.isinf(value):
            return Decimal('0')
    if isinstance(value, str):
        # Remove currency symbols or commas if present
        clean_val = value.replace('£', '').replace('$', '').replace(',', '').strip()
        if clean_val.lower() in ['nan', 'inf', '-inf']:
            return Decimal('0')
        try:
            return Decimal(clean_val)
        except Exception:
            return Decimal('0')
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal('0')

def safe_div(numerator: Union[Decimal, float, str], denominator: Union[Decimal, float, str]) -> Decimal:
    """Divide with zero-check and return Decimal."""
    n = create_decimal(numerator)
    d = create_decimal(denominator)
    if d == 0:
        return Decimal('0')
    return n / d

def quantize_currency(value: Union[Decimal, float, str]) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return create_decimal(value).quantize(PRECISION_CURRENCY, rounding=ROUND_HALF_UP)

def calculate_pnl_percent(current_value: Decimal, total_invested: Decimal) -> float:
    """Calculate PnL percentage safely."""
    if total_invested == 0:
        return 0.0
    return float((current_value - total_invested) / total_invested)