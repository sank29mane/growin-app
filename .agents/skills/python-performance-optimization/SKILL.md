---
name: python-performance-optimization
description: Skills for profiling, analyzing, and optimizing Python code for better performance.
---

# Python Performance Optimization

This skill provides patterns and instructions for optimizing Python code, especially in asynchronous environments like FastAPI and agentic workflows.

## Core Instructions

1. **Profiling First**: Always use `cProfile` or `line_profiler` before optimizing.
2. **Async Safeguards**: Ensure no blocking I/O or heavy CPU tasks run in the main `asyncio` loop. Use `run_in_executor` for CPU-bound tasks.
3. **Pydantic v2**: Leverage `model_validate` and `model_dump` for high-speed serialization.
4. **Memory Management**: Use slots in large classes and clear large data structures explicitly when no longer needed.
5. **Caching**: Implement intelligent multi-layer caching (in-memory + Redis) for frequent market data queries.

## Examples

### Using run_in_executor for CPU-bound tasks
```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

async def run_heavy_task(data):
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor() as pool:
        result = await loop.run_in_executor(pool, heavy_compute_function, data)
    return result
```
