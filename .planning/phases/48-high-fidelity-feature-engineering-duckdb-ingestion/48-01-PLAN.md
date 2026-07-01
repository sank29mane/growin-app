---
phase: 48
plan: 1
wave: 1
---

# Plan 48.1: Vectorized Feature Engineering and Ingestion

## Objective
Optimize DuckDB feature calculations and build high-throughput ingestion for rolling spread, volatility, and volume indicators. Ensure rollups execute under 10ms and parameters are logged to the telemetry database.

## Context
- .planning/REQUIREMENTS.md (PERF-04)
- .planning/phases/48/RESEARCH.md
- backend/analytics_db.py

## Tasks

<task type="auto">
  <name>Implement Vectorized Features in AnalyticsDB</name>
  <files>
    <file>backend/analytics_db.py</file>
  </files>
  <action>
    1. Update DDL in `_init_schema_internal` to create the `market_features` table with `PRIMARY KEY (ticker, timestamp)` and a descending index on `(ticker, timestamp DESC)`.
    2. Implement `calculate_and_ingest_features(self, ticker: str, window: int = 14, correlation_id: Optional[str] = None) -> int`:
       - Run a unified SQL statement using window functions (`AVG` for spread and volume, `STDDEV` for return and volume) to calculate `rolling_spread`, `rolling_volatility`, `rolling_volume_avg`, and `rolling_volume_std` for a specific ticker.
       - The window parameter must be bound dynamically as `window - 1` preceding rows (e.g. `window = 14` -> `ROWS BETWEEN 13 PRECEDING AND CURRENT ROW`).
       - Measure execution time of the query in milliseconds.
       - Log the execution details (ticker, window, rows, and latency) to the `agent_telemetry` table under agent name 'AnalyticsDB' and subject 'feature_calculation'.
  </action>
  <verify>python -c "from analytics_db import get_analytics_db; get_analytics_db()"</verify>
  <done>
    DDL and calculate_and_ingest_features method successfully implemented and syntax-checked.
  </done>
</task>

<task type="auto">
  <name>Create Unit and Performance Tests for Ingestion</name>
  <files>
    <file>tests/backend/test_analytics_db_features.py</file>
  </files>
  <action>
    1. Create a unit test file `tests/backend/test_analytics_db_features.py`.
    2. Seed `ohlcv_history` in an in-memory DuckDB instance with 100+ daily bars of dummy stock data.
    3. Run `calculate_and_ingest_features` with a window of 14.
    4. Assert that:
       - Rolling spread, volatility, and volume statistics are correctly computed and populated in the `market_features` table.
       - The computation and insertion executes in less than 10 milliseconds.
       - A telemetry event is correctly written to the `agent_telemetry` table.
  </action>
  <verify>PYTHONPATH=backend backend/.venv/bin/pytest tests/backend/test_analytics_db_features.py -s</verify>
  <done>
    All unit and performance assertions pass with execution time under 10ms.
  </done>
</task>

## Success Criteria
- [ ] Vectorized feature rollups execute within 10ms in DuckDB.
- [ ] Rolling spread, volatility, volume average, and volume standard deviation are computed correctly.
- [ ] Feature parameters are logged to the telemetry database.
