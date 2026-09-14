## 2025-01-20 - [Pydantic Overhead in Parsing Loops]
**Learning:** Instantiating Pydantic models directly within hot parsing loops just to serialize them immediately (using model_dump()) introduces significant performance overhead, especially when multiplied over hundreds or thousands of elements (e.g. historical price bars).
**Action:** Always construct validated dictionary representations directly in performance-critical data parsing paths (like get_historical_bars), bypassing Pydantic model overhead when the structure and data types are already strictly controlled.
