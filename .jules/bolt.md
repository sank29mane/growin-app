## 2025-05-18 - Avoid Pydantic Instantiation in Parsing Loops
**Learning:** Instantiating Pydantic models within tight data-parsing loops merely to call `.model_dump()` creates immense serialization overhead (up to ~10x slower).
**Action:** When extracting nested structures like PriceData in loops, build direct dictionaries conforming to the expected schema (e.g., Decimal for prices) to save instantiation/dumping overhead. Use overarching TypeAdapters later if explicit validation is still required.
