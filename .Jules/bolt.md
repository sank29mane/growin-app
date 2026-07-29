## 2024-07-29 - [Optimized Decimal Creation]
**Learning:** In a highly numerical codebase, the overhead of type-checking via `isinstance` and inline importing of `math` functions in highly-called primitives like `create_decimal` creates measurable latency. Fast-pathing exact types using `type(x) is T` and caching standard library functions (like `math.isnan`) yields massive speedups.
**Action:** Use fast type checking (`type() is`) for exact primitive types in core data processing pipelines where inheritance isn’t an issue.
