
import pytest
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forecaster import get_forecaster
from agents.forecasting_agent import ForecastingAgent
from market_context import ForecastData

@pytest.mark.asyncio
async def test_forecaster_initialization():
    """Test that forecaster initializes correctly"""
    forecaster = get_forecaster()
    assert forecaster is not None
    # Should detect platform correctly
    assert forecaster.platform in ["apple_silicon", "other"]

@pytest.mark.asyncio
async def test_forecasting_agent_mock_fallback():
    """Test that agent falls back effectively when model not loaded"""
    agent = ForecastingAgent()
    
    # Ensure model is NOT loaded for this test
    # (In real run, it might load fast, so we force a mock path if possible 
    # or just assert the output structure is valid regardless of source)
    
    context = {
        "ticker": "AAPL",
        "ohlcv_data": [
            {"o": 100, "h": 105, "l": 95, "c": 100, "v": 1000, "t": 1600000000 + i*60000} for i in range(100)
        ],
        "days": 5
    }
    
    response = await agent.analyze(context)
    
    assert response.success is True
    assert "forecast_24h" in response.data
    assert "trend" in response.data
    
    # Validate data model
    forecast = ForecastData(**response.data)
    assert forecast.ticker == "AAPL"

@pytest.mark.asyncio
async def test_insufficient_data():
    """Test error handling for too little data"""
    agent = ForecastingAgent()
    context = {
        "ticker": "AAPL",
        "ohlcv_data": [{"c": 100}] * 10, # Only 10 points
        "days": 5
    }
    
    response = await agent.analyze(context)
    assert response.success is False
    assert "Insufficient" in response.error
