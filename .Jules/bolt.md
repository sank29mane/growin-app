## 2025-02-14 - Optimize Portfolio Aggregation
**Learning:** When calculating metrics over dictionaries, using zip over multiple list comprehensions is memory intensive and incurs high object creation overhead (O(N) memory allocation). Using a single iterative pass is significantly faster.
**Action:** Avoid multiple list comprehensions combined with zip for iteration over lists of dictionaries. Use a single pass for loop to extract values and perform aggregation.
