import pytest
from backend.utils.tlh_scanner import TLHScanner
from decimal import Decimal

def test_tlh_scanner_identification():
    """Verify that the scanner identifies positions with significant losses."""
    scanner = TLHScanner(loss_threshold_pct=-5.0, min_loss_amount=50.0)
    
    mock_portfolio = {
        "positions": [
            {
                "ticker": "AAPL",
                "quantity": 10,
                "averagePrice": 150.0,
                "currentPrice": 160.0 # Gain, should skip
            },
            {
                "ticker": "TSLA",
                "quantity": 5,
                "averagePrice": 250.0,
                "currentPrice": 200.0 # -20% loss, £250 total loss. Should identify.
            },
            {
                "ticker": "VOD.L",
                "quantity": 1000,
                "averagePrice": 1.0,
                "currentPrice": 0.98 # -2% loss, £20 total loss. Below threshold. Should skip.
            }
        ]
    }
    
    candidates = scanner.scan(mock_portfolio)
    
    assert len(candidates) == 1
    assert candidates[0]["ticker"] == "TSLA"
    assert candidates[0]["pnl_percent"] == -20.0
    assert candidates[0]["offset_value"] == 250.0
    assert "strategy" in candidates[0]
    
    print(f"TLH Candidate Verified: {candidates[0]['ticker']} with £{candidates[0]['offset_value']} offset.")

if __name__ == "__main__":
    test_tlh_scanner_identification()
    print("TLH Scanner tests passed.")
