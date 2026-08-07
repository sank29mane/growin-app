## 2024-05-24 - Vectorizing Order Book Traversal
**Learning:** In Python simulation models (e.g., MarketImpactModel), iterative 'for' loops that walk the order book are significantly slower than vectorized NumPy operations, especially for large books and numerous iterations.
**Action:** Replace iterative 'for' loops that walk the order book with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' to reduce latency.
