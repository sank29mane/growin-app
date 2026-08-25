## 2024-05-24 - Vectorize Order Book Traversal
**Learning:** In Python simulation models like MarketImpactModel, iterative 'for' loops walking through L2 order book depth array to calculate average fill price add considerable microsecond-level latency, especially given it runs for every single simulated tick.
**Action:** Replace iterative order book traversal with vectorized NumPy operations using `np.cumsum` and `np.searchsorted` along with boolean masks for validity.
