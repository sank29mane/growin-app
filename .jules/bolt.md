## 2026-09-22 - Optimize Pydantic model serialization in tight loops
**Learning:** Instantiating Pydantic models (like `PriceData`) just to immediately serialize them using `.model_dump()` in performance-critical data parsing loops (e.g., in `data_engine.py`) creates significant unnecessary CPU overhead.
**Action:** When parsing incoming data points into dictionaries to be appended to a list, construct the standard Python dictionaries directly, bypassing the expensive Pydantic model instantiation and serialization overhead entirely.
