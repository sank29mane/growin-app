---
name: fastapi-templates
description: Templates and best practices for building robust and scalable FastAPI applications.
---

# FastAPI Templates

This skill provides blueprints for building scalable Python backends for agentic applications.

## Core Instructions

1. **Dependency Injection**: Use FastAPI's `Depends` for shared services (database, cache, agents).
2. **Pydantic Models**: Strictly type all request and response bodies. Enable `extra='forbid'` for strict validation.
3. **Middleware**: Implement global CORS, logging, and error handling middleware.
4. **Project Structure**: Separate routes, services, schemas, and models into distinct modules.
5. **Async Lifecycle**: Use `lifespan` events for initializing and closing expensive resources like MCP clients or model weights.

## Error Handling Pattern
```python
@app.exception_handler(AgentFailure)
async def agent_failure_handler(request: Request, exc: AgentFailure):
    return JSONResponse(
        status_code=500,
        content={"error": "Agent Execution Failed", "detail": str(exc)},
    )
```
