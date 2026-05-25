# Resource Lifecycle Mandates

**Goal:** Ensure 100% system integrity and prevent resource leaks (zombie processes, unclosed TCP sockets, dangling asyncio loops, or memory fragmentation).

---

## 1. Explicit Teardown
- All components that spawn subprocesses (`WorkerClient`, `MultiMCPManager`, `DockerMCPServer`) MUST implement an `async stop()` or `close()` method.
- These methods MUST be called in the `finally` block of the FastAPI `lifespan` or in the `tearDown` of any test.

## 2. Centralized Connection Pool Lifecycle
- The `AgentHttpClient` holds a persistent, shared `httpx.AsyncClient` socket connection pool.
- It MUST implement a clean `close()` method that drains active sockets and shuts down connection tunnels.
- This `close()` method must be called during FastAPI shutdown hooks to ensure all outbound TCP connections are closed cleanly, preventing socket leaks.

## 3. vMLX Server & Serve Lifecycle
- The local vMLX serving server manages GPU/unified memory slots and active KV-caches.
- **Startup Order**: The vMLX serving layer MUST be initialized and report a successful healthy status (`/health`) BEFORE the main FastAPI application or arq background workers start routing agent queries.
- **Shutdown Order**: During system teardown, the main FastAPI application must stop accepting incoming routes first. Then, the background arq queues must be drained, and finally, the vMLX server models must be unloaded and the server stopped, freeing unified memory space cleanly.

## 4. ResourceGuard Cleanup Mandate
- The `ResourceGuard` monitors system memory heartbeats and controls query concurrency limiters.
- Upon service shutdown, the `ResourceGuard` MUST cancel all active wait timers and clear its memory reference arrays to avoid garbage collection leaks.

## 5. Lazy Initialization
- Background services and heavy model weights MUST NOT load or start on module import.
- Use properties or getter methods to initialize heavy components only when they are first needed.
- This prevents the test runner from spawning a full production stack or local LLM serve instances just to run a quick unit test.

## 6. Test Isolation
- Tests MUST NOT touch real external services (Docker, Trading 212 API, HuggingFace downloads) unless explicitly marked as `e2e` or `integration`.
- Mocking MUST be aggressive. If a test fails, it should fail with a traceback, NOT a hang.

## 7. Subprocess Guarding
- Use `utils/process_guard.py` (Parent Watchdog) for all long-running subprocesses.
- If the main Python process dies, the children MUST terminate within 5 seconds.

## 8. Verification
- Verification commands in plans for background tasks MUST include a check for process cleanup.
- Example: `ps aux | grep [process_name]` should be empty after the task is done.
