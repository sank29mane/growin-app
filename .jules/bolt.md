## 2025-01-20 - Vectorizing Order Book Simulation in Python
**Learning:** Python `for` loops in performance-critical areas like order book traversal (`backend/simulation/models.py`) cause huge performance bottlenecks compared to their vectorized counterparts, leading to high latency in calculations that can be reduced to sub-millisecond level.
**Action:** Always replace explicit iterative loops over L2 depth with vectorized numpy operations like `np.cumsum` and `np.searchsorted` to drastically improve performance.
