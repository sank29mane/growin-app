## 2026-07-30 - O(N^2) Nested Loops in Pivot Calculations
**Learning:** The fallback for pivot calculation used slow nested Python loops (O(N*order)) which blocked the main thread for large datasets. Vectorized NumPy boolean masking shifts can do this O(N).
**Action:** Use vectorized boolean shifts for window calculations instead of nested Python loops.

## 2024-05-18 - Avoid unnecessary Pydantic model_dump in tight loops
**Learning:** Instantiating Pydantic models just to call `.model_dump()` in loops is a significant performance bottleneck. When manipulating lists of parsed objects, traversing the object fields directly and manually constructing dicts is much faster.
**Action:** When working with a list of Pydantic models in a tight loop and formatting the response into dicts, avoid calling `.model_dump()` on each model. Instead, access the model attributes directly and construct the dictionaries manually.
