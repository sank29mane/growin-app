"""Statistical forecast path of TTMForecaster.

The previous cases printed pass/fail and still exited 0, including when the
forecast list was empty. One assertion now covers that path. Series stay
under 512 bars so this does not depend on the worker-backed TTM model, which
is not available in CI.
"""

import pytest

from forecaster import TTMForecaster

pytestmark = pytest.mark.asyncio


def _bars(n, step_ms, price_at):
    now = 1_700_000_000_000
    rows = []
    for i in range(n):
        price = float(price_at(i))
        rows.append({
            "t": now - (n - i) * step_ms,
            "o": price,
            "h": price + 1.0,
            "l": price - 1.0,
            "c": price,
            "v": 1000,
        })
    return rows


async def test_statistical_forecast_returns_finite_positive_prices():
    day = 86_400_000
    hour = 3_600_000
    cases = {
        "daily_trend": _bars(80, day, lambda i: 100.0 + i * 0.4),
        "hourly_rally": _bars(80, hour, lambda i: 150.0 + i * 0.5),
        "hourly_drop": _bars(80, hour, lambda i: 100.0 - i * 0.4),
    }
    forecaster = TTMForecaster()
    for name, rows in cases.items():
        timeframe = "1Day" if name.startswith("daily") else "1Hour"
        result = await forecaster.forecast(rows, prediction_steps=3, timeframe=timeframe)
        forecast = result.get("forecast") or []
        assert len(forecast) == 3, (name, result.get("error"), result.get("note"))
        closes = [row["close"] for row in forecast]
        assert all(isinstance(price, float) and price > 0 for price in closes), (name, closes)
        last = rows[-1]["c"]
        # The statistical fallback stays near the last price. This is the check
        # the old print-only anchoring tests described and never enforced.
        assert abs(closes[0] - last) / last < 0.15, (name, closes[0], last)
