## 2026-09-20 - Pydantic model serialization overhead in tight loops
**Learning:** Instantiating Pydantic models only to immediately call `.model_dump()` on them within large loops (like parsing financial tick data) introduces significant serialization and validation overhead, creating a severe performance bottleneck.
**Action:** When parsing thousands of records into dictionaries, bypass Pydantic model instantiation and construct the target dictionaries directly to drastically reduce CPU usage and parsing time.
