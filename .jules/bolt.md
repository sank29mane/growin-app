## 2024-05-24 - Vectorize Order Book Traversal
**Learning:** Iterative loops walking the order book in simulation models cause a 10x latency overhead compared to vectorized NumPy equivalents, especially as depth arrays grow.
**Action:** When calculating slippage or order impact in `MarketImpactModel`, replace `for` loops with boolean masking (`sizes > 0`) followed by `np.cumsum` and `np.searchsorted` to quickly index the exact fill level, always ensuring to check `if len(cum_sizes) > 0:` after masking.
