## 2024-09-05 - Vectorize MarketImpactModel Order Book Traversal
**Learning:** In Python simulation models (e.g., MarketImpactModel), iterative 'for' loops that walk the order book can cause performance bottlenecks.
**Action:** Replace iterative 'for' loops with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' to significantly reduce latency. Also handle edge cases of empty valid arrays after boolean masking by explicitly extracting fallback values from original unmasked arrays.
