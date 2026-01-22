"""
Currency Normalization Service
Handles proper display of prices in their correct denominations
"""
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def get_display_price(price: float, currency_code: str) -> tuple[float, str]:
    """
    Get price in correct display format with currency symbol.
    
    Args:
        price: Raw price value from Trading212
        currency_code: Currency code (GBX, GBP, USD, EUR, etc.)
    
    Returns:
        Tuple of (display_price, currency_symbol)
        - GBX: returns (price, 'p') for pence
        - GBP: returns (price, '£') for pounds
        - USD: returns (price, '$')
        - EUR: returns (price, '€')
    """
    currency_code = currency_code.upper()
    
    if currency_code == 'GBX':
        # Pence - display as-is with 'p' symbol
        return (price, 'p')
    elif currency_code == 'GBP':
        # Pounds - display as-is with '£' symbol
        return (price, '£')
    elif currency_code == 'USD':
        return (price, '$')
    elif currency_code == 'EUR':
        return (price, '€')
    else:
        # Unknown currency - return as-is with code
        return (price, currency_code)


def normalize_to_base_currency(price: float, currency_code: str) -> float:
    """
    Normalize price to base currency (GBP) for calculations.
    Only used for portfolio totals and P&L calculations.
    
    Args:
        price: Raw price value
        currency_code: Currency code from Trading212
    
    Returns:
        Price in GBP for calculations
    """
    currency_code = currency_code.upper()
    
    if currency_code == 'GBX':
        # Convert pence to pounds for calculations
        return price / 100.0
    elif currency_code == 'GBP':
        # Already in pounds
        return price
    else:
        # For other currencies, return as-is
        # TODO: Add exchange rate conversion if needed
        return price


def normalize_position(position: Dict[str, Any], instrument_metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize position prices for display and calculations.
    
    Adds display fields while preserving raw values:
    - currentPriceDisplay: formatted for UI
    - currentPriceCurrency: currency symbol
    - currentPriceGBP: normalized for calculations
    
    Args:
        position: Position data from Trading212
        instrument_metadata: Instrument metadata with currency codes
    
    Returns:
        Position with added display fields
    """
    ticker = position.get('ticker')
    if not ticker or ticker not in instrument_metadata:
        logger.warning(f"No metadata for ticker {ticker}, using defaults")
        return position
    
    currency = instrument_metadata[ticker].get('currency', 'GBP')
    
    # Process current price
    if 'currentPrice' in position:
        raw_price = position['currentPrice']
        display_price, currency_symbol = get_display_price(raw_price, currency)
        normalized_price = normalize_to_base_currency(raw_price, currency)

        # Replace original price with normalized value for frontend compatibility
        position['currentPrice'] = normalized_price
        position['currentPriceDisplay'] = display_price
        position['currentPriceCurrency'] = currency_symbol
        position['currentPriceGBP'] = normalized_price  # Keep for backend calculations
    
    # Process average price
    if 'averagePrice' in position:
        raw_price = position['averagePrice']
        display_price, currency_symbol = get_display_price(raw_price, currency)
        normalized_price = normalize_to_base_currency(raw_price, currency)

        # Replace original price with normalized value for frontend compatibility
        position['averagePrice'] = normalized_price
        position['averagePriceDisplay'] = display_price
        position['averagePriceCurrency'] = currency_symbol
        position['averagePriceGBP'] = normalized_price  # Keep for backend calculations
    
    # P&L is already in base currency from Trading212
    if 'ppl' in position:
        position['pplGBP'] = position['ppl']
    
    # Store original currency for reference
    position['currency'] = currency
    
    return position


def normalize_all_positions(positions: List[Dict[str, Any]], instrument_metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize all positions in a list.
    
    Args:
        positions: List of position dicts
        instrument_metadata: Instrument metadata cache
    
    Returns:
        List of normalized positions
    """
    return [normalize_position(pos, instrument_metadata) for pos in positions]


def calculate_portfolio_value(positions: List[Dict[str, Any]]) -> float:
    """
    Calculate total portfolio value in GBP.
    Uses normalized prices for accurate totals.
    
    Args:
        positions: List of normalized positions
    
    Returns:
        Total value in GBP
    """
    total = 0.0
    for pos in positions:
        price_gbp = pos.get('currentPriceGBP', pos.get('currentPrice', 0))
        quantity = pos.get('quantity', 0)
        total += price_gbp * quantity
    
    return total
