import sys
import os
from decimal import Decimal

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.currency_utils import CurrencyNormalizer
from price_validation import PriceValidator
from data_models import Position

def test_decimal_precision():
    """Test that we avoid floating point errors."""
    # Float error example: 0.1 + 0.2 != 0.3
    assert 0.1 + 0.2 != 0.3
    
    # Decimal correctness
    d1 = Decimal('0.1')
    d2 = Decimal('0.2')
    assert d1 + d2 == Decimal('0.3')

def test_pence_to_pounds():
    """Test safe conversion from pence to pounds."""
    # 1234 pence = 12.34 pounds
    val = CurrencyNormalizer.pence_to_pounds(1234)
    assert isinstance(val, Decimal)
    assert val == Decimal('12.34')
    
    # Float input
    val = CurrencyNormalizer.pence_to_pounds(1234.567) # Should round to 12.35
    assert val == Decimal('12.35')

def test_normalize_price():
    """Test price normalization logic."""
    # UK stock
    price = CurrencyNormalizer.normalize_price(1550, "LLOY.L")
    assert price == Decimal('15.50')
    
    # US stock
    price = CurrencyNormalizer.normalize_price(150.50, "AAPL")
    assert price == Decimal('150.50')

def test_position_model():
    """Test Position model creation and calculations."""
    raw_pos = {
        "ticker": "LLOY.L",
        "quantity": 1000,
        "currentPrice": 5000, # 5000 pence = £50.00
        "averagePrice": 4000, # 4000 pence = £40.00
        "currency": "GBX"
    }
    
    pos = CurrencyNormalizer.normalize_position(raw_pos)
    
    assert isinstance(pos, Position)
    assert pos.current_price == Decimal('50.00')
    assert pos.average_price == Decimal('40.00')
    assert pos.quantity == Decimal('1000')
    
    # Calculated values
    # Market Value: 50.00 * 1000 = 50000.00
    assert pos.market_value == Decimal('50000.00')
    
    # P&L: (50 - 40) * 1000 = 10000.00
    assert pos.unrealized_pnl == Decimal('10000.00')
    
    # P&L Percent: 10000 / 40000 * 100 = 25%
    assert pos.unrealized_pnl_percent == Decimal('25.0')

def test_variance_calculation():
    """Test PriceValidator variance with Decimals."""
    prices = {
        "source1": Decimal('100.00'),
        "source2": Decimal('101.00'), # 1% diff
        "currency": "USD"
    }
    
    result = PriceValidator.calculate_variance(prices)
    
    # Mean = 100.5
    # Variance source1: abs(100 - 100.5) / 100.5 * 100 = 0.5 / 100.5 * 100 ~= 0.4975%
    # Variance source2: abs(101 - 100.5) / 100.5 * 100 ~= 0.4975%
    
    # With Decimal precision, we expect consistent results
    assert result['mean_price'] == Decimal('100.50')
    # Max variance should be around 0.5%
    assert result['max_variance'] < Decimal('1.0')
