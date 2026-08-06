## 2024-05-19 - Vectorizing order book traversal
**Learning:** In Python simulation models that walk an L2 order book (like `MarketImpactModel`), python `for` loops create significant overhead (e.g., looping over 1000s of levels for each simulation tick). Using vectorized operations `np.cumsum` combined with `np.searchsorted` avoids loops entirely and cuts execution time by over 50%.
**Action:** Always replace iterative 'for' loops that accumulate quantities in order books with vectorized NumPy operations (`np.cumsum` and `np.searchsorted`) in high-frequency contexts like quant models.
