"""Shared helpers for tests that need a specific regime from the SHIPPED GMM artifact.

A raw component id means nothing by itself. Tests that want "full size" ask for the
calmest component through the severity map, never for a literal raw id.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from coreml.gmm_loader import load_gmm_params
from simulation.regime_severity import RegimeSeverityMap, build_severity_map

ARTIFACT = Path(__file__).resolve().parents[2] / "models" / "gmm_regime_params.npz"
# The sha256 of the shipped npz, from the defect note. A retrain changes it and fails loudly.
ARTIFACT_SHA256 = "154172baa4d634f6dd380bc18139317151f63b5331f81cf6f96bcd1c5903e823"


def shipped_params() -> dict:
    return load_gmm_params(str(ARTIFACT))


def shipped_map() -> RegimeSeverityMap:
    return build_severity_map(shipped_params())


def one_hot(raw_id: int, count: int = 4) -> np.ndarray:
    vector = np.zeros(count)
    vector[raw_id] = 1.0
    return vector


def calm_probabilities() -> np.ndarray:
    """Posterior that puts all mass on the calmest component of the shipped model."""

    return one_hot(shipped_map().calm_id)


def rank_probabilities(rank: int) -> np.ndarray:
    """Posterior that puts all mass on the component of the given severity rank."""

    return one_hot(shipped_map().id_by_rank[rank])


def permuted_params(order: tuple[int, ...]) -> dict:
    """The shipped model with its components renumbered: new id i is old component order[i]."""

    params = shipped_params()
    index = list(order)
    return {
        "weights": params["weights"][index],
        "means": params["means"][index],
        "precisions_cholesky": params["precisions_cholesky"][index],
        "scaler_mean": params["scaler_mean"],
        "scaler_var": params["scaler_var"],
    }


def bound_regime_fields(raw_id: int | None = None) -> dict:
    """The regime kwargs an admission must carry: raw id, policy hash and a matching audit.

    ``raw_id`` defaults to the calm component. The audit is built from the trusted map plus a
    model version, exactly the shape the classifier's evidence produces.
    """

    severity_map = shipped_map()
    raw = severity_map.calm_id if raw_id is None else raw_id
    return {
        "regime_id": raw,
        "regime_policy_hash": severity_map.policy_hash,
        "regime_audit": {**severity_map.audit(raw), "model_version": f"gmm-p256:{ARTIFACT_SHA256}"},
    }


_SHARED_POLICY = {}


def gated(**kwargs) -> dict:
    """ExecutionService keyword arguments for a gated, bound service: the real risk gate and
    the shipped model's trusted severity map. Every admission is sized by the gate now."""

    from simulation import RiskSwarmGate

    return {"risk_gate": RiskSwarmGate(), "regime_severity_map": shipped_map(), **kwargs}


def bound_admit(raw_id: int | None = None, *, spread: float = 0.002) -> dict:
    """``admit``/``prepare`` keyword arguments that bind an admission to the shipped model.

    The default is the calm component, which the real gate sizes at 1.0, so a test that asks
    for N shares still gets N. The policy table is built once from the shipped map and only
    read, never modified; a test that needs a different table builds its own.
    """

    from simulation.regime_severity import build_scaling_policy_connection

    connection = _SHARED_POLICY.get("connection")
    if connection is None:
        connection = build_scaling_policy_connection(shipped_map())
        _SHARED_POLICY["connection"] = connection
    return {**bound_regime_fields(raw_id), "current_spread_pct": spread, "risk_db_connection": connection}


class FractionGate:
    """The real risk gate with its sizing multiplier fixed by the test.

    For tests of what happens AFTER a gate has scaled an order (re-checks at the admitted
    quantity, the SELL waiver). Everything else is the real gate: it still verifies the policy
    table against the trusted map, still enforces the 5% spread block and still returns zero
    when the real gate would. Only a non-zero result is replaced by ``size * fraction``.
    """

    def __init__(self, fraction: float) -> None:
        from simulation import RiskSwarmGate

        self.fraction = fraction
        self._gate = RiskSwarmGate()

    def evaluate(self, fill, size, regime_id, spread, connection, severity_map=None):
        real = self._gate.evaluate(fill, size, regime_id, spread, connection, severity_map=severity_map)
        return 0.0 if real == 0.0 else size * self.fraction
