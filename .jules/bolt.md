## 2024-05-24 - Vectorize MarketImpactModel slippage calculation
**Learning:** Using a `for` loop to walk order book depth arrays is significantly slower than using vectorized NumPy operations, especially for large trade sizes and deep books.
**Action:** Replace iterative depth walk with `np.cumsum` and `np.searchsorted` in Python quantitative models.
