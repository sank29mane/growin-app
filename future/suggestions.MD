# Strategic Suggestions

## Detailed Action Plan

| Priority | Area | Issue | Proposed Solution | Status |
|----------|------|-------|-------------------|--------|
| High | Backend / Database | Blocking SQLite Calls | The `ChatManager` uses synchronous `sqlite3` calls (e.g., `execute`, `commit`) within async endpoints. This blocks the main event loop, degrading performance under load. | Migrate to `aiosqlite` for truly asynchronous database access, or wrap all DB calls in `run_in_executor`. | Proposed |
