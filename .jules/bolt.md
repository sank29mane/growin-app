## 2024-05-18 - Optimize Pydantic instantiation in high-throughput parsing
**Learning:** Pydantic model instantiation (like `model_validate`) incurs non-trivial overhead when run inside tight loops (like portfolio allocation parsers).
**Action:** Avoid intermediate Pydantic object creation in performance-critical execution paths. Instead, call validation logic (like `validate_and_convert`) directly on dictionaries or rely on overarching schemas.
