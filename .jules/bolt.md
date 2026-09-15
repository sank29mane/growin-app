## 2023-10-27 - Python Dictionary Aggregation Overhead
**Learning:** Using multiple list comprehensions to extract values from a list of dictionaries, followed by `zip` to combine them, creates significant memory overhead and multiple O(N) passes.
**Action:** Always iterate through the list of dictionaries directly in a single pass when computing aggregate sums or metrics to avoid intermediate list creation.
