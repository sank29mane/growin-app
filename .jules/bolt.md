## 2024-05-15 - Vectorized Order Book Traversal
**Learning:** In Python simulation models (e.g., MarketImpactModel), replacing iterative 'for' loops that walk the order book with vectorized NumPy operations using 'np.cumsum' and 'np.searchsorted' significantly reduces latency.
**Action:** Replace iterative order book traversal with `np.cumsum` and `np.searchsorted`, but always use boolean masking for validity checks (e.g., filtering out zero/negative sizes) beforehand, and include guardrails for empty arrays (e.g., `if len(cum_sizes) > 0:`).
