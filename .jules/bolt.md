
## 2026-08-28 - Vectorized Order Book Traversal
**Learning:** In Python simulation models (e.g., MarketImpactModel), iterative 'for' loops that walk the order book are extremely slow. They can be effectively replaced with vectorized NumPy operations using `np.cumsum` and `np.searchsorted` along with boolean masking for validity checks (e.g., filtering out zero/negative sizes).
**Action:** When working on performance optimizations in Python, proactively look for iterative loops over arrays/lists and replace them with vectorized NumPy operations where possible. Always include guardrails for empty valid arrays.
