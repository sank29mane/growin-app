## 2024-05-18 - NumPy Vectorization for Order Book Traversal
**Learning:** In highly iterated simulation contexts like `MarketImpactModel`, using standard Python `for` loops combined with `zip()` to walk deep arrays (e.g., L2 order book prices and sizes) creates immense interpreter overhead.
**Action:** Replace iterative order book walking logic with vectorized NumPy operations using `np.cumsum` and `np.searchsorted`, remembering to use boolean masking first (`sizes > 0`) to handle non-contributing levels and guard against empty masks (`if len(valid_sizes) == 0:`).
