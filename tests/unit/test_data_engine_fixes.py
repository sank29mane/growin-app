import unittest
from unittest.mock import MagicMock, patch
import sys
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import asyncio

# --- MOCKS SETUP BEFORE IMPORT ---
sys.modules['alpaca'] = MagicMock()
sys.modules['alpaca.trading'] = MagicMock()
sys.modules['alpaca.trading.client'] = MagicMock()
sys.modules['alpaca.data'] = MagicMock()
sys.modules['alpaca.data.historical'] = MagicMock()
sys.modules['finnhub'] = MagicMock()

# Mock utils dependencies
mock_utils_ticker = MagicMock()
mock_utils_ticker.normalize_ticker = lambda x: x
sys.modules['utils.ticker_utils'] = mock_utils_ticker

mock_utils_currency = MagicMock()
# Explicitly define normalize_price to return Decimal
mock_utils_currency.CurrencyNormalizer.normalize_price = lambda p, t: Decimal(str(p))
mock_utils_currency.CurrencyNormalizer.is_uk_stock = lambda t: False
sys.modules['utils.currency_utils'] = mock_utils_currency

mock_data_models = MagicMock()
class MockPriceData:
    def __init__(self, **kwargs): self.dict = kwargs
    def model_dump(self): return self.dict
mock_data_models.PriceData = MockPriceData
sys.modules['data_models'] = mock_data_models

# Mock circuit breaker
mock_resilience = MagicMock()
def mock_circuit_breaker(*args, **kwargs):
    def decorator(func):
        return func
    return decorator
mock_resilience.circuit_breaker = mock_circuit_breaker
mock_resilience.CircuitBreaker = MagicMock()
sys.modules['utils.error_resilience'] = mock_resilience

# Mock cache_manager
sys.modules['cache_manager'] = MagicMock()

# --- IMPORT MODULE UNDER TEST ---
from backend.data_engine import AlpacaClient, FinnhubClient

class TestDataEngineFixes(unittest.TestCase):

    @patch('backend.data_engine.API_KEY', 'test_key')
    @patch('backend.data_engine.API_SECRET', 'test_secret')
    def test_get_portfolio_positions_merged(self):
        # We need to run async test in sync wrapper
        asyncio.run(self._test_get_portfolio_positions_merged_async())

    async def _test_get_portfolio_positions_merged_async(self):
        client = AlpacaClient()
        client.trading_client = MagicMock()

        # Create mock position object with string values that Decimal can parse
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_pos.qty = "10"
        mock_pos.market_value = "1500.00"
        mock_pos.cost_basis = "1400.00"
        mock_pos.unrealized_pl = "100.00"
        mock_pos.unrealized_plpc = "0.07"
        mock_pos.current_price = "150.00"
        mock_pos.lastday_price = "145.00"
        mock_pos.change_today = "0.03"
        mock_pos.side = "long"

        # Ensure hasattr returns False for avg_entry_price to test calculation fallback
        del mock_pos.avg_entry_price
        # Note: MagicMock by default creates attributes on access, so 'del' ensures hasattr fails?
        # Actually MagicMock with spec might be better, but let's try strict deletion or configuration
        # Alternatively, we can just leave it as is if we didn't set it, checking hasattr on MagicMock can be tricky.
        # A safer way is using a simple class or namedtuple

        class SimplePos:
            def __init__(self):
                self.symbol = "AAPL"
                self.qty = "10"
                self.market_value = "1500.00"
                self.cost_basis = "1400.00"
                self.unrealized_pl = "100.00"
                self.unrealized_plpc = "0.07"
                self.current_price = "150.00"
                self.lastday_price = "145.00"
                self.change_today = "0.03"
                self.side = "long"

        simple_pos = SimplePos()

        # Mock the synchronous call
        client.trading_client.get_all_positions.return_value = [simple_pos]

        # Patch asyncio.to_thread to run the function immediately
        # Because we can't easily patch the one imported in data_engine if it used 'from asyncio import to_thread'
        # But it uses 'import asyncio', so patching 'backend.data_engine.asyncio.to_thread' works
        with patch('backend.data_engine.asyncio.to_thread', side_effect=lambda func, *args: func(*args)) as mock_thread:
            positions = await client.get_portfolio_positions()

        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p['symbol'], "AAPL")
        self.assertEqual(p['qty'], Decimal("10"))
        # 1400 / 10 = 140
        self.assertEqual(p['avg_entry_price'], Decimal("140.00"))
        self.assertEqual(p['side'], "long")
        self.assertEqual(p['current_price'], Decimal("150.00"))

        # Test with explicit avg_entry_price
        simple_pos.avg_entry_price = "142.00"
        with patch('backend.data_engine.asyncio.to_thread', side_effect=lambda func, *args: func(*args)):
            positions = await client.get_portfolio_positions()
        self.assertEqual(positions[0]['avg_entry_price'], Decimal("142.00"))

    @patch('backend.data_engine.FINNHUB_API_KEY', 'test_finnhub_key')
    def test_finnhub_historical_bars_timeframe(self):
         asyncio.run(self._test_finnhub_historical_bars_timeframe_async())

    async def _test_finnhub_historical_bars_timeframe_async(self):
        client = FinnhubClient()
        client.client = MagicMock()
        client.client.stock_candles.return_value = {'s': 'ok', 'c': [100], 't': [1600000000], 'o': [100], 'h': [100], 'l': [100], 'v': [100]}

        # Test 1Day logic
        limit = 10
        await client.get_historical_bars("AAPL", timeframe="1Day", limit=limit)
        args, kwargs = client.client.stock_candles.call_args
        _from = kwargs.get('_from')
        to = kwargs.get('to')

        diff = datetime.fromtimestamp(to, tz=timezone.utc) - datetime.fromtimestamp(_from, tz=timezone.utc)
        # Expected: limit + 30 days = 40 days
        self.assertTrue(timedelta(days=39) <= diff <= timedelta(days=41))

        # Test 1Min logic (limit * 1 min)
        client.client.stock_candles.reset_mock()
        client.client.stock_candles.return_value = {'s': 'ok', 'c': [100], 't': [1600000000], 'o': [100], 'h': [100], 'l': [100], 'v': [100]}
        limit = 60
        await client.get_historical_bars("AAPL", timeframe="1Min", limit=limit)
        args, kwargs = client.client.stock_candles.call_args
        _from = kwargs.get('_from')
        to = kwargs.get('to')
        diff = datetime.fromtimestamp(to, tz=timezone.utc) - datetime.fromtimestamp(_from, tz=timezone.utc)
        # Expected: 60 mins
        self.assertTrue(timedelta(minutes=59) <= diff <= timedelta(minutes=61))

if __name__ == '__main__':
    unittest.main()
