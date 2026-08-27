## 2024-05-24 - Vectorizing Order Book Traversal in MarketImpactModel
**Learning:** Iterating through depth arrays in Python (using `for` loops) causes unnecessary latency during dense order book traversal in `MarketImpactModel`. Vectorizing the walk with boolean masking (`sizes > 0`), `np.cumsum`, and `np.searchsorted` avoids expensive Python-level loop overhead.
**Action:** In Python simulation models dealing with 1D arrays or sequence aggregations, replace iterative Python loops with vectorized NumPy operations to significantly reduce execution latency.
