## 2024-08-05 - Vectorizing Book Walking
**Learning:** In high-frequency simulators, a Python `for` loop iterating over price/size tuples in the order book can be a significant latency bottleneck.
**Action:** Replace `for` loops in order book traversal with `np.cumsum` and `np.searchsorted` for a measurable speedup.
