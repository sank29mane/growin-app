from datetime import date
from decimal import Decimal
from data_models import DividendData

def test_dividend_data_schema():
    # T212 style
    t212_data = {
        "ticker": "AAPL",
        "amount": "0.24",
        "ex_date": "2024-02-09",
        "status": "CONFIRMED",
        "currency": "USD"
    }
    div1 = DividendData(**t212_data)
    assert div1.ticker == "AAPL"
    assert div1.amount == Decimal("0.24")
    assert div1.ex_date == date(2024, 2, 9)

    # Alpaca style
    alpaca_data = {
        "ticker": "MSFT",
        "amount": 0.75,
        "ex_date": "2024-02-14",
        "payment_date": "2024-03-14",
        "record_date": "2024-02-15",
        "frequency": "QUARTERLY"
    }
    div2 = DividendData(**alpaca_data)
    assert div2.ticker == "MSFT"
    assert div2.amount == Decimal("0.75")
    assert div2.payment_date == date(2024, 3, 14)
    assert div2.record_date == date(2024, 2, 15)
