| Problem | Implication | Proposed Solution | Status |
| --- | --- | --- | --- |
| Inefficient Dataframe concatenation in `backtest_lab/run_comparison.py` | O(N^2) time complexity, scales poorly for large datasets. | Replace iterative `pd.concat` with list accumulation and single `pd.concat` at the end. | Unresolved |
