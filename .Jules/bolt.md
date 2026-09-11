## 2026-07-30 - O(N^2) Nested Loops in Pivot Calculations
**Learning:** The fallback for pivot calculation used slow nested Python loops (O(N*order)) which blocked the main thread for large datasets. Vectorized NumPy boolean masking shifts can do this O(N).
**Action:** Use vectorized boolean shifts for window calculations instead of nested Python loops.
