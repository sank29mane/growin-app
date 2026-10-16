"""Mac-side India risk module (Phase 63-02 and 63-04, RISK-01 to RISK-03, P-14, P-18).

``rules``, ``drawdown`` and ``exits`` are pure Decimal code: order rules, the Option B latch
machine and the exit batches. Nothing in them reads a file, a clock or the network, calls a
broker, or imports from ``gateway/``. The VM enforces the same limits with its own
implementation; shared vectors, not shared code, keep the two in agreement.

``state`` (63-04) is the one module that does file I/O: the durable latch file beside the
India ledger. ``__main__`` is the operator reset CLI. Both are scanned by their own source
test, and the admission wiring lives in ``execution/india_guard.py``, not here.
"""
