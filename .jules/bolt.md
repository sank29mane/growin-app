## 2024-09-06 - Vectorize order book traversal in MarketImpactModel
**Learning:** Iterative loops over numpy arrays in Python simulation models (like order book traversal) are slow.
**Action:** Replace `for` loops with vectorized numpy operations using boolean masking, `np.cumsum`, and `np.searchsorted` to improve performance. Always handle empty valid arrays and extract fallback prices from original arrays to avoid index errors.
