## 2026-01-18 - yfinance Timezone Handling & Iterrows Bottleneck
**Learning:** `yfinance` returns TimeZone-aware DatetimeIndex (usually America/New_York). Naive timestamp calculation (subtracting `pd.Timestamp("1970-01-01")`) fails with `TypeError`.
**Action:** Always check `df.index.tz` before timestamp conversion. Use `pd.Timestamp("1970-01-01", tz="UTC")` for TZ-aware indices and naive epoch for naive indices to be robust. Also, `iterrows` loops over DataFrames are a significant bottleneck (~10x slower than vectorization) and should be replaced with vectorized operations where possible, even if it requires extracting logic from helper classes.

## 2026-01-18 - Portfolio History Vectorization
**Learning:** `iterrows` in Pandas is significantly slower (5-7x) than vectorized operations (`df.dot`, `np.where`) for portfolio value calculations.
**Action:** When calculating weighted sums or conditional logic across time-series dataframes, always prefer:
1. `np.where` for element-wise conditionals (like currency conversion).
2. `df.dot(weights)` for weighted sums.
3. Ensure strict DataFrame types (convert Series with `to_frame()`) before applying these operations.
4. Operate on copies (`df.copy()`) if modifying data to avoid side effects or `SettingWithCopyWarnings`.
