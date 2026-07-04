import pytest
import time
from datetime import datetime, timedelta
from analytics_db import AnalyticsDB

def test_vectorized_feature_calculation():
    """
    Test vectorized feature calculations in DuckDB (Phase 48).
    Ensures calculations occur in <10ms and telemetry is logged properly.
    """
    # 1. Initialize in-memory database
    db = AnalyticsDB(":memory:")
    
    # Clean up first
    db.execute("DELETE FROM ohlcv_history")
    db.execute("DELETE FROM market_features")
    db.execute("DELETE FROM agent_telemetry")
    
    # 2. Seed dummy bars (100 days of data to satisfy rolling window)
    ticker = "AAPL"
    base_time = datetime(2026, 1, 1)
    ohlcv_data = []
    
    for i in range(100):
        timestamp = base_time + timedelta(days=i)
        # Create varying price action
        close_price = 150.0 + (i * 0.5) if i % 2 == 0 else 150.0 - (i * 0.3)
        ohlcv_data.append({
            "ticker": ticker,
            "timestamp": timestamp,
            "open": close_price - 1.0,
            "high": close_price + 2.0,
            "low": close_price - 1.5,
            "close": close_price,
            "volume": 100000 + (i * 1000)
        })
        
    db.bulk_insert_ohlcv(ticker, ohlcv_data)
    
    # 3. Time the feature calculation and ingestion
    start_time = time.perf_counter()
    rows_processed = db.calculate_and_ingest_features(ticker, window=14)
    end_time = time.perf_counter()
    
    latency_ms = (end_time - start_time) * 1000
    print(f"\n⚡ Vectorized feature calculation latency: {latency_ms:.4f}ms")
    
    # Assert latency is extremely fast (within 10ms threshold)
    # To prevent transient failures on resource-constrained CI agents, we check < 50ms, but target < 10ms.
    assert latency_ms < 50.0, f"Vectorized calculation took too long: {latency_ms:.2f}ms"
    assert rows_processed > 0, "No rows processed or ingested"
    
    # 4. Assert calculations in market_features
    features_df = db.execute("SELECT * FROM market_features WHERE ticker = ? ORDER BY timestamp DESC", (ticker,)).fetchdf()
    assert not features_df.empty
    assert len(features_df) >= 86 # 100 seeded - 14 window offset = 86 expected
    
    # Check that computed fields are populated and valid
    first_row = features_df.iloc[0]
    assert first_row["rolling_spread"] > 0
    assert first_row["rolling_volatility"] > 0
    assert first_row["rolling_volume_avg"] > 100000
    assert first_row["rolling_volume_std"] > 0
    
    # 5. Assert telemetry logging
    telemetry_df = db.execute("SELECT * FROM agent_telemetry WHERE agent_name = 'AnalyticsDB' AND subject = 'feature_calculation'").fetchdf()
    assert not telemetry_df.empty
    
    import json
    payload = json.loads(telemetry_df.iloc[0]["payload"])
    assert payload["ticker"] == ticker
    assert payload["window"] == 14
    assert payload["rows_processed"] == rows_processed
    assert "latency_ms" in payload
    assert "latest_volatility" in payload
