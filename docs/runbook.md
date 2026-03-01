# GSD Runbook

> Operational procedures for debugging, validation, and recovery.

---

## Quick Commands

### Status Check

**PowerShell:**
```powershell
# Current git status
git status

# Recent commits
git log --oneline -10

# Current branch
git branch --show-current
```

**Bash:**
```bash
# Current git status
git status

# Recent commits
git log --oneline -10

# Current branch
git branch --show-current
```

## Phase 19-20: SOTA 2026 Operations

### Unified Test Suite Execution
**Goal:** Run the consolidated SOTA integration tests safely.
```bash
# Run all backend tests from the project root
export PYTHONPATH=$PYTHONPATH:backend
uv run --project backend pytest tests/backend/

# Run specific Phase 19 Integration Logic
uv run --project backend pytest tests/integration/agent_revision/
```

### Alpha Audit Verification
**Goal:** Verify that DuckDB is correctly correlating agent reasoning with forward returns.
1. Run a test session via the app or curl.
2. Check DuckDB records:
```bash
uv run python -c "from backend.analytics_db import get_analytics_db; print(get_analytics_db().get_agent_alpha_metrics())"
```

### Jules Swarm Synchronization
**Goal:** Safely poll and apply background tasks from the Jules worker.
1. Check active remote sessions:
```bash
jules remote list --session
```
2. **Safety Audit (Mandatory):** Pull patch locally before applying.
```bash
jules remote pull --session <ID> > audit.patch
cat audit.patch # Review diff for stale logic
```
3. Apply approved patch:
```bash
patch -p1 < audit.patch
```

---

## Phase 16: Hardware Optimization

### Hardware Optimization (M4 Pro)
**Goal:** Verify 8-bit AFFINE quantization is active for local inference.

1.  **Check Backend Logs:**
    ```bash
    grep "Applying 8-bit AFFINE" growin_server.log
    ```
2.  **Monitor NPU/GPU usage:** Use `Activity Monitor` (macOS) -> `GPU History` to ensure balanced load during Orchestrator execution.

### Critic Pattern & HITL Verification
**Goal:** Ensure trade suggestions are audited and require manual signatures.

1.  **Trigger Risk Audit:** Query the chat with a reckless trade (e.g. "Buy £1M of TSLA").
2.  **Verify Risk Warning:** Confirm the response includes `⚠️ RISK WARNING` and `[ACTION_REQUIRED:TRADE_APPROVAL]`.
3.  **Test Trade Gate:** Attempt to call a trade tool via `/mcp/tool/call` without an `approval_token`. It MUST return `403 Forbidden`.

---

## System Health & Observability

### Telemetry Verification
**Goal:** Confirm real-time agentic reasoning trace is flowing from backend to frontend.

1.  **Monitor intelligent Console:** Open the macOS app and navigate to "Vital Monitor".
2.  **Verify SSE Stream:**
    ```bash
    curl -N -H "Accept: text/event-stream" http://localhost:8002/api/chat/message -d '{"message": "Analyze AAPL"}'
    ```
    Ensure `event: telemetry` packets are visible.
3.  **Neural Pathway Pulse:** Confirm specialized nodes (Quant, Research, Forecast) transition to `working` state during a live query.

### Python Sandbox (Docker) Operations
**Goal:** Manage and verify secure mathematical modeling.

1.  **Check Sandbox Readiness:**
    ```bash
    docker ps | grep growin-npu
    ```
2.  **Manual Test Execution:**
    ```python
    # Use the docker_run_python tool via MCP with engine="npu"
    result = await mcp.call_tool("docker_run_python", {"script": "print(2+2)", "engine": "npu"})
    ```
3.  **Resource Cleanup:** The system automatically removes containers on completion. If runaway containers are detected:
    ```bash
    docker rm -f $(docker ps -a -q --filter "ancestor=growin-npu-compute")
    ```

---

## Wave Validation

### Verify Wave Completion

**Before marking a wave complete:**

1. All tasks have commits:
   ```powershell
   git log --oneline -N  # N = number of tasks in wave
   ```

2. All verifications passed (documented in SUMMARY.md)

3. STATE.md updated with current position

4. State snapshot created

### Wave Rollback

**If a wave needs to be reverted:**

```powershell
# Find commit before wave started
git log --oneline -20

# Reset to that commit (keeps changes staged)
git reset --soft <commit-hash>

# Or hard reset (discards changes)
git reset --hard <commit-hash>
```

---

## Debugging Procedures

### 3-Strike Rule

After 3 consecutive failed debug attempts:

1. **Stop** — Don't try a 4th approach in same session

2. **Document** in STATE.md:
   ```markdown
   ## Debug Session
   
   **Problem:** {description}
   
   **Attempts:**
   1. {approach 1} → {result}
   2. {approach 2} → {result}
   3. {approach 3} → {result}
   
   **Hypothesis:** {current theory}
   
   **Recommended next:** {suggested approach}
   ```

3. **Fresh session** — Start new conversation with documented context

### Log Inspection

**Find relevant logs:**

```powershell
# Search for error patterns
Select-String -Path "*.log" -Pattern "error|exception|failed" -CaseSensitive:$false
```

```bash
# Search for error patterns
grep -ri "error\|exception\|failed" *.log
```

---

## Verification Commands

### Build Verification

```powershell
# Node.js
npm run build
if ($LASTEXITCODE -eq 0) { Write-Host "✅ Build passed" }

# Python
python -m py_compile src/**/*.py
```

### Test Verification

```powershell
# Node.js
npm test

# Python
pytest -v

# Go
go test ./...
```

### Lint Verification

```powershell
# Node.js
npm run lint

# Python
ruff check .

# Go
golangci-lint run
```

---

## State Recovery

### From STATE.md

When resuming work:

1. Read STATE.md for current position
2. Check "Last Action" for context
3. Follow "Next Steps" to continue
4. Verify recent commits match documented progress

### From Git History

If STATE.md is outdated:

```powershell
# See recent work
git log --oneline -20

# Check specific commit details
git show <commit-hash> --stat

# View file at specific commit
git show <commit-hash>:path/to/file
```

### Context Pollution Recovery

If quality is degrading mid-session:

1. Create state snapshot immediately
2. Update STATE.md with full context
3. Commit any pending work
4. Start fresh session
5. Run `/resume` to reload context

---

## Search Commands

### Find in Codebase

**PowerShell:**
```powershell
# Find pattern in files
Select-String -Path "src/**/*.ts" -Pattern "TODO" -Recurse

# Find files by name
Get-ChildItem -Recurse -Filter "*.config.*"
```

**Bash:**
```bash
# Find pattern in files (with ripgrep)
rg "TODO" --type ts

# Find pattern in files (with grep)
grep -r "TODO" src/

# Find files by name
find . -name "*.config.*"
```

### Search-First Workflow

Before reading any file:

1. Search for relevant terms:
   ```powershell
   Select-String -Path "**/*.md" -Pattern "architecture" -Recurse
   ```

2. Identify candidate files from results

3. Read only relevant sections:
   ```powershell
   Get-Content file.md | Select-Object -Skip 49 -First 20  # Lines 50-70
   ```

---

## Common Issues

### "SPEC.md not FINALIZED"

**Cause:** Planning lock prevents implementation

**Fix:**
1. Open `.gsd/SPEC.md`
2. Complete all required sections
3. Change status to `Status: FINALIZED`
4. Retry command

### "Context degrading"

**Symptoms:** Shorter responses, skipped steps, inconsistency

**Fix:**
1. Create state snapshot
2. Commit current work
3. Start fresh session
4. Run `/resume`

### "Commit failed"

**Causes:** Staged conflicts, hook failures

**Debug:**
```powershell
git status
git diff --staged
```

---

## Checklist Templates

### Pre-Execution Checklist

- [ ] SPEC.md is FINALIZED
- [ ] ROADMAP.md has current phase
- [ ] STATE.md loaded and understood
- [ ] Previous wave verified complete

### Post-Wave Checklist

- [ ] All tasks committed
- [ ] Verifications documented
- [ ] STATE.md updated
- [ ] State snapshot created
- [ ] No uncommitted changes

### Session End Checklist

- [ ] Current work committed
- [ ] STATE.md has "Next Steps"
- [ ] JOURNAL.md updated (if milestone)
- [ ] No loose ends

---

*See PROJECT_RULES.md for canonical rules.*
*See docs/model-selection-playbook.md for model guidance.*
