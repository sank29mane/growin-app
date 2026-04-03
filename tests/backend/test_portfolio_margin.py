import pytest
from decimal import Decimal
from quant_engine import QuantEngine, PortfolioMarginManager, SimulationEngine

def test_sa_ccr_margin_calculation():
    sim = SimulationEngine()
    manager = PortfolioMarginManager(sim)
    
    positions = [
        {"ticker": "AAPL", "qty": 100, "current_price": 190, "asset_class": "EQUITY"},
        {"ticker": "MSFT", "qty": 50, "current_price": 400, "asset_class": "EQUITY"}
    ]
    
    result = manager.calculate_sa_ccr_margin(positions)
    
    # Portfolio Value = 19000 + 20000 = 39000
    # RC = 39000
    # PFE = 39000 * 0.10 = 3900
    # EAD = 1.4 * (39000 + 3900) = 1.4 * 42900 = 60060
    # Required Margin = 60060 * 0.20 = 12012
    
    assert result["rc"] == Decimal("39000")
    assert result["pfe"] == Decimal("3900")
    assert result["sa_ccr_ead"] == Decimal("60060")
    assert result["required_margin"] == Decimal("12012")

def test_epe_calculation():
    sim = SimulationEngine()
    manager = PortfolioMarginManager(sim)
    
    epe = manager.calculate_epe("AAPL", 100.0, 0.2)
    
    assert isinstance(epe, Decimal)
    # EPE should be positive as long as there's some volatility
    assert epe > 0

def test_quant_engine_margin_interface():
    engine = QuantEngine()
    positions = [
        {"ticker": "AAPL", "qty": 10, "current_price": 190}
    ]
    
    margin = engine.calculate_portfolio_margin(positions)
    assert "required_margin" in margin
    assert margin["required_margin"] > 0
