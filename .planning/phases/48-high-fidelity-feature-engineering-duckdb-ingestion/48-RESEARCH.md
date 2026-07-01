# RESEARCH: Vectorized Feature Engineering in DuckDB

## Context
In GSD Milestone v6.0, we prioritize system performance and alpha accuracy. For Phase 48, the target is to move rolling feature calculations (rolling spread, volatility, and volume indicators) directly into DuckDB's vectorized engine to bypass Python loop/Pandas overhead, ensuring feature ingestion and calculation complete within 10ms.

## DuckDB Performance Advantage
DuckDB is a columnar database designed for high-performance analytics (OLAP). Window calculations in DuckDB are written in C++ and execute using vectorized operations (SIMD vector pipelines). This allows calculating rolling standard deviations and averages across thousands of rows in sub-milliseconds, whereas the equivalent loop or Pandas `.rolling()` calls in Python are bound by the GIL and require significant memory allocation/copying.

## Vectorized SQL Design

We will implement the rolling computations using a single, unified SQL statement in [analytics_db.py](file:///Users/sanketmane/Codes/Growin%20App/backend/analytics_db.py):

```sql
INSERT INTO market_features (ticker, timestamp, rolling_spread, rolling_volatility, rolling_volume_avg, rolling_volume_std)
WITH raw_features AS (
    SELECT 
        ticker,
        timestamp,
        ((high - low) / NULLIF(close, 0)) as spread,
        (close - LAG(close) OVER (PARTITION BY ticker ORDER BY timestamp)) / 
            NULLIF(LAG(close) OVER (PARTITION BY ticker ORDER BY timestamp), 0) as daily_return,
        volume
    FROM ohlcv_history
    WHERE ticker = ?
),
calculated_features AS (
    SELECT
        ticker,
        timestamp,
        AVG(spread) OVER (PARTITION BY ticker ORDER BY timestamp ROWS BETWEEN ? PRECEDING AND CURRENT ROW) as rolling_spread,
        STDDEV(daily_return) OVER (PARTITION BY ticker ORDER BY timestamp ROWS BETWEEN ? PRECEDING AND CURRENT ROW) as rolling_volatility,
        AVG(volume) OVER (PARTITION BY ticker ORDER BY timestamp ROWS BETWEEN ? PRECEDING AND CURRENT ROW) as rolling_volume_avg,
        STDDEV(volume) OVER (PARTITION BY ticker ORDER BY timestamp ROWS BETWEEN ? PRECEDING AND CURRENT ROW) as rolling_volume_std
    FROM raw_features
)
SELECT ticker, timestamp, rolling_spread, rolling_volatility, rolling_volume_avg, rolling_volume_std
FROM calculated_features
WHERE rolling_volatility IS NOT NULL
ON CONFLICT (ticker, timestamp) DO UPDATE SET
    rolling_spread = EXCLUDED.rolling_spread,
    rolling_volatility = EXCLUDED.rolling_volatility,
    rolling_volume_avg = EXCLUDED.rolling_volume_avg,
    rolling_volume_std = EXCLUDED.rolling_volume_std;
```

## Telemetry Logging

To fulfill the second success criteria, all feature calculations must be logged to the telemetry database (`agent_telemetry` table) with the following structure:
```json
{
  "ticker": "AAPL",
  "window": 14,
  "rows_processed": 1000,
  "latency_ms": 1.24
}
```

This telemetry log will be created with `agent_name="AnalyticsDB"` and `subject="feature_calculation"`.
