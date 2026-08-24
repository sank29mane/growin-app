## 2024-08-24 - MarketImpactModel Vectorization
**Learning:** In Python simulation models (e.g., MarketImpactModel), iterative 'for' loops that walk the order book are a significant performance bottleneck.
**Action:** Replace iterative 'for' loops that walk the order book with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' to significantly reduce latency.
