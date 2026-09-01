## 2024-11-20 - Vectorize Order Book Traversal
**Learning:** In Python simulation models (e.g., MarketImpactModel), iterative 'for' loops that walk the order book are a significant performance bottleneck due to Python overhead.
**Action:** Replace iterative 'for' loops with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' along with boolean masking to significantly reduce latency.
