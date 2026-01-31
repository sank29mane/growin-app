## 2026-01-18 - yfinance Timezone Handling & Iterrows Bottleneck
**Learning:** `yfinance` returns TimeZone-aware DatetimeIndex (usually America/New_York). Naive timestamp calculation (subtracting `pd.Timestamp("1970-01-01")`) fails with `TypeError`.
**Action:** Always check `df.index.tz` before timestamp conversion. Use `pd.Timestamp("1970-01-01", tz="UTC")` for TZ-aware indices and naive epoch for naive indices to be robust. Also, `iterrows` loops over DataFrames are a significant bottleneck (~10x slower than vectorization) and should be replaced with vectorized operations where possible, even if it requires extracting logic from helper classes.

## 2026-01-18 - Vectorization of Portfolio History
**Learning:** Vectorizing the portfolio history calculation (replacing `iterrows`) yielded a 5x speedup (0.03s -> 0.006s for 365 days). Handling currency conversion logic vectorially using `np.where` is efficient.
**Action:** When optimizing financial data processing, always look for per-row logic (like currency conversion based on ticker/price) and convert it to column-wise vector operations. Ensure inputs are consistently DataFrames (handle Series edge case from `yfinance`).

## 2025-01-20 - SQLite Optimization: Cursor Iteration & Indexing
**Learning:** `cursor.fetchall()` loads the entire result set into memory, creating an intermediate list. Direct cursor iteration is more memory-efficient. Adding a composite index `(conversation_id, timestamp DESC)` improved `load_history` performance by ~14x (3.2ms -> 0.23ms) for 100 concurrent conversations.
**Action:** Prefer direct cursor iteration over `fetchall()`. Always add composite indexes for columns involved in both filtering (`WHERE`) and sorting (`ORDER BY`) to enable efficient index scans.

## 2026-01-18 - Vectorization of EMA (Recursive vs Convolution)
**Learning:** `pandas.Series.ewm(span=N, adjust=False)` provides ~15x speedup (0.07s -> 0.005s) over iterative Python loops for EMA calculation. However, `np.convolve` (used for SMA) is faster than `pandas.Series.rolling` (0.0038s vs 0.0055s) because it uses optimized C-level convolution.
**Action:** Use Pandas `ewm` for recursive indicators like EMA (matching initialization carefully), but stick to `np.convolve` for simple moving averages / convolutions where possible. Always verify speedups before switching to Pandas for simple operations.

## 2026-01-20 - Exception Handling Overhead in Loops
**Learning:** Checking for an optional dependency using `try...import...except` *inside* a frequently called function (like `normalize_ticker`) adds massive overhead (~2µs per call) if the dependency is missing, due to repeated exception raising.
**Action:** Move conditional imports to module scope and use a boolean flag (`HAS_DEPENDENCY`) to guard the optional logic. This reduced execution time by ~32% and nearly matched pure Python performance.
