"""Mac-side India risk module (Phase 63-02, RISK-01 to RISK-03, P-14).

Pure Decimal code: order rules, the Option B latch machine and the exit batches.
Nothing here reads a file, a clock or the network, calls a broker, or imports from
``gateway/``. The VM enforces the same limits with its own implementation; shared
vectors, not shared code, keep the two in agreement.

Execution wiring (the dispatcher seam, the latch file, the admin CLI) is Phase 63-04.
"""
