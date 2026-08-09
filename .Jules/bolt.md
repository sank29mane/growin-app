## 2024-05-28 - Optimize order book simulation
**Learning:** In Python simulation models processing order books, iterative `for` loops that walk the depth levels cause significant latency.
**Action:** Replace iterative order book walking with vectorized NumPy operations using `np.cumsum` and `np.searchsorted` to dramatically reduce latency and keep execution time under the sub-millisecond level.
