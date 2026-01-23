## 2026-01-18 - yfinance Timezone Handling & Iterrows Bottleneck
**Learning:** `yfinance` returns TimeZone-aware DatetimeIndex (usually America/New_York). Naive timestamp calculation (subtracting `pd.Timestamp("1970-01-01")`) fails with `TypeError`.
**Action:** Always check `df.index.tz` before timestamp conversion. Use `pd.Timestamp("1970-01-01", tz="UTC")` for TZ-aware indices and naive epoch for naive indices to be robust. Also, `iterrows` loops over DataFrames are a significant bottleneck (~10x slower than vectorization) and should be replaced with vectorized operations where possible, even if it requires extracting logic from helper classes.

## 2026-01-18 - Pandas Vectorization Scope & Fallbacks
**Learning:** Replacing `iterrows` with vectorized operations yielded ~15x speedup. However, I learned that local imports (like `import numpy as np`) inside loops, when removed, can cause `NameError` if the module wasn't imported globally. Also, empty DataFrames in vectorization (e.g. `df[[]].dot([])`) correctly return zero-filled structures, eliminating the need for explicit "if empty" checks.
**Action:** When vectorizing, ensure libraries (numpy/pandas) are imported globally. Trust vector algebra for empty cases but verify behavior. Avoid introducing new fallback logic when optimizing; preserve exact original behavior.
