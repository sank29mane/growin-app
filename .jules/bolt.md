## 2024-08-30 - MarketImpactModel Vectorization
**Learning:** Python simulation models (e.g., MarketImpactModel) iterating over order books with 'for' loops are extremely slow.
**Action:** Replace iterative 'for' loops that walk the order book with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' to significantly reduce latency. Handle the edge case of an empty valid array (e.g., checking 'if len(cum_sizes) > 0:' or 'if len(valid_sizes) == 0:') after boolean masking to prevent index bounds errors on empty subsets.
