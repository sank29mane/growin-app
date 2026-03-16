# Resource Lifecycle Mandates

**Goal:** Ensure 100% system integrity and prevent resource leaks (zombie processes, unclosed sockets, dangling asyncio loops).

## 1. Explicit Teardown
- All components that spawn subprocesses (`WorkerClient`, `MultiMCPManager`, `DockerMCPServer`) MUST implement an `async stop()` or `close()` method.
- These methods MUST be called in the `finally` block of the FastAPI `lifespan` or in the `tearDown` of any test.

## 2. Lazy Initialization
- Background services MUST NOT start on module import.
- Use properties or getter methods to initialize heavy components only when they are first needed.
- This prevents the test runner from spawning a full production stack just to run a unit test.

## 3. Test Isolation
- Tests MUST NOT touch real external services (Docker, Trading 212 API, HuggingFace downloads) unless explicitly marked as `e2e` or `integration`.
- Mocking MUST be aggressive. If a test fails, it should fail with a traceback, NOT a hang.

## 4. Subprocess Guarding
- Use `utils/process_guard.py` (Parent Watchdog) for all long-running subprocesses.
- If the main Python process dies, the children MUST terminate within 5 seconds.

## 5. Verification
- Verification commands in `PLAN.md` for background tasks MUST include a check for process cleanup.
- Example: `ps aux | grep [process_name]` should be empty after the task is done.
