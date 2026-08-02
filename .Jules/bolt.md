## 2024-08-02 - Optimize multiple list comprehensions into single loop
**Learning:** When aggregating values over a list of dictionaries (like portfolio metrics), using multiple list comprehensions combined with `zip` creates unnecessary O(N) intermediate lists. A single O(N) single-pass loop reduces memory overhead to O(1) and is faster.
**Action:** When calculating derived totals from dictionary lists, extract and aggregate fields directly in a single `for` loop rather than creating pre-parsed lists.
