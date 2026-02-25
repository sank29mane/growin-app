
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from decimal import Decimal
import sys
import os
import asyncio

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from server import app
from schemas import GoalPlanContext, MLXDownloadRequest, GoalExecutionRequest
from utils.currency_utils import CurrencyNormalizer

client = TestClient(app)

# --- 1. Currency Normalization Tests ---
def test_currency_normalization_logic():
    """Verify CurrencyNormalizer correctly handles LSE vs US stocks."""
    
    # LSE Stock (Pence to Pounds)
    # 1234 GBX -> 12.34 GBP
    # Note: Logic returns Decimal
    assert CurrencyNormalizer.normalize_price(1234, "LLOY.L") == Decimal("12.34")
    
    # LSE Stock already low (Unlikely case, but check no double division if logic assumes high value)
    # The logic is likely: if UK -> pence_to_pounds (/100). 
    # Wait, check strict logic: is_uk_stock -> pence_to_pounds.
    # 550 GBX -> 5.50 GBP
    assert CurrencyNormalizer.normalize_price(550, "VOD.L") == Decimal("5.50")
    
    # US Stock (No conversion)
    # 150.00 USD -> 150.00
    assert CurrencyNormalizer.normalize_price(150.00, "AAPL") == Decimal("150.00")
    
    # Penny Stock logic check (UK)
    # 45 GBX -> 0.45 GBP
    assert CurrencyNormalizer.normalize_price(45, "PEN.L") == Decimal("0.45")

# --- 2. API Pydantic Refactor Tests ---

@patch('agents.goal_planner_agent.GoalPlannerAgent')
def test_goal_plan_endpoint(mock_agent_cls):
    """Test /api/goal/plan uses Pydantic model correctly."""
    mock_instance = mock_agent_cls.return_value
    
    # Mock return object
    mock_response = MagicMock()
    mock_response.success = True
    mock_response.data = {"plan": "Execute Order 66"}
    
    # Make analyze return an awaitable (Future)
    f = asyncio.Future()
    f.set_result(mock_response)
    mock_instance.analyze.return_value = f
    
    # Correct payload matching GoalPlanContext schema
    payload = {
        "initial_capital": 10000,
        "target_returns_percent": 8.5,
        "duration_years": 5,
        "risk_profile": "AGGRESSIVE_PLUS",
        "purpose": "Retirement"
    }
    
    response = client.post("/api/goal/plan", json=payload)
    
    assert response.status_code == 200, f"Response: {response.text}"
    assert response.json() == {"plan": "Execute Order 66"}
    
    # Verify the mock was called with correct data
    # Note: we need to check if analyze was called.
    # Since we mocked the class, mock_instance is the instance created inside the route.
    assert mock_instance.analyze.called
    called_args = mock_instance.analyze.call_args[0][0]
    # Pydantic model_dump returns floats for float fields
    assert called_args["initial_capital"] == 10000.0
    assert called_args["risk_profile"] == "AGGRESSIVE_PLUS"

def test_goal_plan_validation_error():
    """Test that missing required fields raises 422 validation error."""
    payload = {
        "risk_profile": "high"
        # Missing initial_capital, etc.
    }
    response = client.post("/api/goal/plan", json=payload)
    assert response.status_code == 422  # Pydantic Validation Error

@patch('routes.agent_routes.download_mlx_model') 
def test_mlx_download_endpoint_schema(mock_download):
    """Test /api/models/mlx/download uses correct schema."""
    # Since we are mocking the route function itself here (if we patch the route handler), 
    # actually patch ignores the implementation.
    # To test validation, we don't need to patch the route, just call it.
    # But downloading does real work.
    # Let's patch the implementation inside the route if possible, or just expect the queued response.
    # The route is async.
    
    # Actually, easier: The route just returns a dict. It doesn't call an external service really
    # except maybe logger? No, let's look at code.
    # It just returns {"status": "queued"...}
    # So we don't need to patch anything logic-heavy.
    
    payload = {"repo_id": "mlx-community/Mistral-7B-v0.1-4bit"}
    response = client.post("/api/models/mlx/download", json=payload)
    
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert "Mistral-7B" in response.json()["message"]

def test_mlx_download_validation():
    """Test validation for MLX download."""
    response = client.post("/api/models/mlx/download", json={}) # Missing repo_id
    assert response.status_code == 422

# --- 3. Data Engine Account Info Test ---

def test_account_info_gbp_default():
    """Test that get_account_info returns GBP currency by default."""
    from data_engine import AlpacaClient
    
    client = AlpacaClient()
    # Mock the internal trading_client
    client.trading_client = MagicMock()
    
    # Create the mock account object returned by SDK
    mock_acct = MagicMock()
    mock_acct.cash = "10000.00"
    mock_acct.currency = "GBP" # SDK usually returns 'GBP' if account is GBP
    # But strict check: our code fallback dictates "GBP" if key missing?
    # No, code says: acct.get("currency", "GBP")
    # So if SDK returns a dict with currency, it uses it. if SDK returns object with .currency, it uses it.
    # Let's test the fallback path or the normal path.
    # Normal path:
    
    # We need to mock returning a dict-like object or object with attributes
    # The code handles both. Let's return a dict to be safe and clear.
    mock_acct_dict = {
        "cash": "10000.00",
        "currency": "GBP",
        "portfolio_value": "15000.00",
        "equity": "15000.00",
        "last_equity": "14000.00",
        "buying_power": "10000.00",
        "status": "ACTIVE"
    }
    
    # Mock asyncio.to_thread to return this dict immediately
    # We must patch asyncio.to_thread in the context where data_engine imports it or uses it.
    
    with patch('data_engine.asyncio.to_thread', new=MagicMock(return_value=asyncio.Future())) as mock_to_thread:
        # Configure the future to have a result
        f = asyncio.Future()
        f.set_result(mock_acct_dict)
        mock_to_thread.return_value = f
        
        # Run the async method
        # But wait, to_thread is awaited. So it expects an Awaitable?
        # calling await on a Future works.
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        result = loop.run_until_complete(client.get_account_info())
        
        assert result['cash_balance']['currency'] == "GBP"
        assert result['status'] == "ACTIVE"
        loop.close()

if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
