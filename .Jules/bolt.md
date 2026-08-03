## 2026-08-03 - O(1) Portfolio Metrics
**Learning:** Multiple list comprehensions and zips over large lists of dictionaries (like portfolio positions) incur O(N) memory overhead and slow down aggregation loops.
**Action:** Use a single O(1) for-loop with direct extraction and aggregation to improve memory and speed.
