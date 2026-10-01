## 2024-05-18 - Avoid unnecessary Pydantic model_dump in tight loops
**Learning:** Instantiating Pydantic models just to call `.model_dump()` in loops is a significant performance bottleneck. When manipulating lists of parsed objects, traversing the object fields directly and manually constructing dicts is much faster.
**Action:** When working with a list of Pydantic models in a tight loop and formatting the response into dicts, avoid calling `.model_dump()` on each model. Instead, access the model attributes directly and construct the dictionaries manually.
