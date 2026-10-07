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
