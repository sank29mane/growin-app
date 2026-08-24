# Future Suggestions & SOTA Roadmap

| SOTA Ref | Component | Observation | Detailed Action Plan | Risk |
|---|---|---|---|---|
| 2026-08-24 | QuantEngine | Market Impact loops are slow | Replace iterative for loops in MarketImpactModel with vectorized `np.cumsum` and `np.searchsorted` to slash execution time and memory overhead from O(N) to O(1) vectorized space. | Low |
| 2026-08-24 | QuantEngine | Moving average loops are slow | Replace list comprehension iteration for metric rollups with a single-pass `for` loop that extracts fields and aggregates in one vectorized motion. | Low |
| 2026-08-24 | Telemetry | Incomplete trace logging | Add robust execution bounds to agent run loops to trace non-happy paths natively inside the global `AgentHttpClient`. | Low |
| 2026-08-24 | QuantEngine | Out-of-bounds in masking | Ensure vectorized boolean masks for time-series pivots include guardrails for `len(array) <= order * 2` to prevent early `ValueError` fallback. | Low |
| 2026-08-24 | Forecaster | TimesFM Model Benchmark | Standalone CatBoost and Hybrid TimesFM-CatBoost achieved over 220 ops/sec on NSE 5m data. For scaling, replacing heavy foundation model calls with CatBoost ensembles (optionally with mocked/light JMCE) hits the 10 ops/sec requirement easily while retaining low MAE. | Low |
