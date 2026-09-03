## 2024-06-25 - Vectorize Order Book Traversal
**Learning:** In Python simulation models, iterative 'for' loops that walk the order book can be a significant latency bottleneck. Replacing them with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' significantly reduces execution time.
**Action:** Use boolean masking and `np.cumsum` with `np.searchsorted` for cumulative aggregations over arrays instead of `for` loops.
