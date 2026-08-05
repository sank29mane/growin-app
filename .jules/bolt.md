## 2024-05-19 - [Avoid multiple list comprehensions combined with zip for dictionary aggregations]
**Learning:** When optimizing Python aggregation over lists of dictionaries (e.g., portfolio metrics), avoid using multiple list comprehensions combined with `zip`. A single `for` loop that extracts fields and aggregates values directly in one pass is significantly faster and reduces memory overhead from O(N) to O(1).
**Action:** Use a single loop to compute sum directly instead of constructing multiple temporary lists.
