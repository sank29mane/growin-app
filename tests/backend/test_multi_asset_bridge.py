import pytest
from unittest.mock import patch, AsyncMock
from decimal import Decimal
from data_fabricator import DataFabricator
from data_models import AssetType

@pytest.mark.asyncio
async def test_fabricator_crypto_routing():
    with patch("data_engine.AlpacaClient.get_crypto_bars") as mock_crypto:
        mock_crypto.return_value = {
            "bars": [{"t": 1600000000000, "o": "100", "h": "105", "l": "95", "c": "102", "v": "1000"}],
            "ticker": "BTC/USD"
        }
        
        fabricator = DataFabricator()
        # Mock other dependencies to avoid side effects
        fabricator.alpaca.get_real_time_quote = AsyncMock(return_value=None)
        fabricator._fetch_news_data = AsyncMock(return_value=None)
        fabricator._fetch_social_data = AsyncMock(return_value=None)
        fabricator._fetch_macro_indicators = AsyncMock(return_value={"vix_level": Decimal("20"), "yield_spread_10y2y": Decimal("1")})
        fabricator._fetch_geopolitical_data = AsyncMock(return_value=None)
        
        ctx = await fabricator.fabricate_context(intent="swing_trade", ticker="BTC/USD", account_type="invest")
        
        mock_crypto.assert_called_once()
        assert ctx.price is not None
        assert ctx.price.ticker == "BTC/USD"
        # The logic sets AssetType.EQUITY by default in PriceData, wait, we might need to verify the price object properties
        assert ctx.price.current_price == Decimal("102") # Fallback to last close

@pytest.mark.asyncio
async def test_fabricator_option_routing():
    with patch("data_engine.AlpacaClient.get_option_bars") as mock_option:
        mock_option.return_value = {
            "bars": [{"t": 1600000000000, "o": "10", "h": "15", "l": "9", "c": "12", "v": "500"}],
            "ticker": "AAPL240621C00150000"
        }
        
        fabricator = DataFabricator()
        fabricator.alpaca.get_real_time_quote = AsyncMock(return_value=None)
        fabricator._fetch_news_data = AsyncMock(return_value=None)
        fabricator._fetch_social_data = AsyncMock(return_value=None)
        fabricator._fetch_macro_indicators = AsyncMock(return_value={"vix_level": Decimal("20"), "yield_spread_10y2y": Decimal("1")})
        fabricator._fetch_geopolitical_data = AsyncMock(return_value=None)
        
        ctx = await fabricator.fabricate_context(intent="swing_trade", ticker="AAPL240621C00150000", account_type="invest")
        
        mock_option.assert_called_once()
        assert ctx.price is not None
        assert ctx.price.ticker == "AAPL240621C00150000"

@pytest.mark.asyncio
async def test_fabricator_fx_routing():
    with patch("data_engine.FinnhubClient.get_fx_rates") as mock_fx:
        mock_fx.return_value = {
            "bars": [{"t": 1600000000000, "o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15", "v": "0"}],
            "ticker": "OANDA:EUR_USD"
        }
        
        fabricator = DataFabricator()
        fabricator.alpaca.get_real_time_quote = AsyncMock(return_value=None)
        fabricator.finnhub.get_real_time_quote = AsyncMock(return_value=None)
        fabricator._fetch_news_data = AsyncMock(return_value=None)
        fabricator._fetch_social_data = AsyncMock(return_value=None)
        fabricator._fetch_macro_indicators = AsyncMock(return_value={"vix_level": Decimal("20"), "yield_spread_10y2y": Decimal("1")})
        fabricator._fetch_geopolitical_data = AsyncMock(return_value=None)
        
        ctx = await fabricator.fabricate_context(intent="swing_trade", ticker="OANDA:EUR_USD", account_type="invest")
        
        mock_fx.assert_called_once()
        assert ctx.price is not None
        assert ctx.price.ticker == "OANDA:EUR_USD"

@pytest.mark.asyncio
async def test_research_agent_option_underlying():
    from agents.research_agent import ResearchAgent
    agent = ResearchAgent()
    
    with patch.object(agent, "_fetch_newsapi") as mock_newsapi:
        mock_newsapi.return_value = [{"title": "AAPL News", "description": "Good", "source": {"name": "Test"}, "url": "http"}]
        agent.tavily_key = None
        agent.newsdata_key = None
        agent.newsapi_key = "test_key_valid_long_enough"
        
        res = await agent.analyze({"ticker": "AAPL240621C00150000"})
        
        # Verify it searched for 'AAPL' not the full option string
        mock_newsapi.assert_called_once_with("AAPL", "AAPL", "stock")
        assert res.success == True
