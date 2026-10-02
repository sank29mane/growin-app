"""Daily-bar costs and fills for the India workspace (Phase 60).

Contract of this package:

* Pure Decimal arithmetic. No float, no clock, no RNG, no network and no broker
  access. Every public entry point runs inside ``COST_CONTEXT`` so a process
  that mutates the global decimal context cannot change a result.
* Deterministic. Identical inputs give identical canonical JSON and an
  identical run hash. Every estimate and run carries the schedule version and
  its sha256 hash.
* Fail closed. A missing tick size, band, schedule date or malformed input
  raises or produces an explicit no-assumed-fill outcome. Nothing defaults.
* Nothing here imports ``backend/execution``, ``backend/simulation``,
  ``backend/market_data`` or ``backend/pilot_data``.

``backend/simulation/engine.py`` and ``backend/simulation/models.py`` stay
untouched and are for tick-data pre-flight only: their float maths, L2-depth
dependence and per-run random latency break the determinism required here.

Nothing is re-exported from this module, so later plans never need to edit it.
"""
