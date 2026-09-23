## 2024-05-18 - Avoid unnecessary Pydantic model instantiations just to serialize to dict
**Learning:** Instantiating Pydantic models (like `PriceData`) only to immediately call `.model_dump()` within data processing loops (e.g. data_engine.py building bars) is highly inefficient. It adds validation and parsing overhead just to get a dictionary.
**Action:** Construct the dictionary directly instead of using `PriceData(...).model_dump()` in hot paths where a dictionary is the end goal.
