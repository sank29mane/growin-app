## 2026-08-24 - [Forecaster Optimization]
**Learning:** Foundational models (TimesFM, Kronos) have immense parameter overhead, causing them to fall well below the 10 ops/sec threshold on raw un-batched inference during intraday evaluation.
**Action:** Always prefer hybrid gradient-boosted ensembles (CatBoost + Structural Lags) or lightweight linear projections over the embedding space. This scales inference vertically (up to ~220 ops/sec) with minimal accuracy degradation (MAE < 1.5).
## 2026-08-24 - [Benchmark Integration Learnings]
**Learning:** During benchmarking, replacing foundation model inputs with structural mocks during training causes extreme distribution mismatch (training on MA, predicting with real TimesFM output). This destroys benchmark validity.
**Action:** Always extract real model embeddings/predictions uniformly across the entire dataset (train and predict slices) to maintain identical feature distributions when chaining outputs to downstream gradient boosting heads. Ensure gracefully fallback handles edge cases natively in both loops to prevent crashing when heavy dependencies (CatBoost, TimesFM) are absent.
