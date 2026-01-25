## 2026-01-18 - yfinance Timezone Handling & Iterrows Bottleneck
**Learning:** `yfinance` returns TimeZone-aware DatetimeIndex (usually America/New_York). Naive timestamp calculation (subtracting `pd.Timestamp("1970-01-01")`) fails with `TypeError`.
**Action:** Always check `df.index.tz` before timestamp conversion. Use `pd.Timestamp("1970-01-01", tz="UTC")` for TZ-aware indices and naive epoch for naive indices to be robust. Also, `iterrows` loops over DataFrames are a significant bottleneck (~10x slower than vectorization) and should be replaced with vectorized operations where possible, even if it requires extracting logic from helper classes.

## 2026-01-18 - Vectorization of Portfolio History
**Learning:** Vectorizing the portfolio history calculation (replacing `iterrows`) yielded a 5x speedup (0.03s -> 0.006s for 365 days). Handling currency conversion logic vectorially using `np.where` is efficient.
**Action:** When optimizing financial data processing, always look for per-row logic (like currency conversion based on ticker/price) and convert it to column-wise vector operations. Ensure inputs are consistently DataFrames (handle Series edge case from `yfinance`).
