# Quick Task: sysctl-based Memory Guard

Implement Step 1 of Phase 27: Create a sysctl-based Memory Guard in `backend/memory_guard.py` using the 60% RAM/4GB free hard-gate strategy identified in research.

## Plan
1.  **Initialize:** Check system parameters and existing memory management.
2.  **Implementation:**
    -   Create `backend/memory_guard.py`.
    -   Implement `get_memory_stats()` using `sysctl`.
    -   Implement `check_memory_safety()` with a 60% usage hard-gate and a 4GB free hard-gate.
    -   Handle macOS specific `sysctl` keys (`hw.memsize`, `vm.page_free_count`, `hw.pagesize`).
3.  **Test:** Create `tests/backend/test_memory_guard.py` and verify guards correctly report safety.
4.  **Integration:** (Optional) Show how to use it in other components.
5.  **State Update:** Update `.gsd/STATE.md` and `.planning/STATE.md`.
6.  **Commit:** Atomic commit of `backend/memory_guard.py` and its test.

## Decisions
-   **Method:** Use `sysctl` for maximum efficiency and direct system visibility.
-   **Page Size:** Use `hw.pagesize` for correct conversions (16KB on M4 Pro/Max).
-   **Total RAM:** Use `hw.memsize`.
-   **Free RAM:** Use `vm.page_free_count` + `vm.page_speculative_count` as a proxy for "immediately available" memory. (Note: On macOS, "Free" is usually much lower than "Available", but for a hard-gate, conservative "Free" is safer).
-   **Hard-Gate Strategy:**
    -   `used_ram > (total_ram * 0.6)` -> Trigger Guard (Memory High).
    -   `free_ram < (4 * 1024 * 1024 * 1024)` -> Trigger Guard (Memory Low).
