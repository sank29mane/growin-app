## 2024-05-15 - Vectorize Order Book Walk

**Learning:** The Python backend heavily loops over limit order book states. An iterative `for` loop combined with `zip(prices, sizes)` to calculate slippage is significantly slower (by almost an order of magnitude) than vectorizing the depth walking via `np.cumsum` and `np.searchsorted`.
**Action:** In Python simulation models dealing with cumulative sums across lists or arrays, especially order book models, avoid iterating and applying conditionals inside the loop. Instead, mask the arrays and use NumPy's vectorization capabilities (`np.cumsum` and `np.searchsorted`) to substantially reduce latency.
