## 2026-07-30 - O(N^2) Nested Loops in Pivot Calculations
**Learning:** The fallback for pivot calculation used slow nested Python loops (O(N*order)) which blocked the main thread for large datasets. Vectorized NumPy boolean masking shifts can do this O(N).
**Action:** Use vectorized boolean shifts for window calculations instead of nested Python loops.

## 2023-10-27 - Python Dictionary Aggregation Overhead
**Learning:** Using multiple list comprehensions to extract values from a list of dictionaries, followed by `zip` to combine them, creates significant memory overhead and multiple O(N) passes.
**Action:** Always iterate through the list of dictionaries directly in a single pass when computing aggregate sums or metrics to avoid intermediate list creation.
