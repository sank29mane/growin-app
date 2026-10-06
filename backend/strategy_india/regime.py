"""GMM regime filter (D-09). The only module that may use numpy, scikit-learn or float.

Reuses the BIC-over-K pipeline idea from ``scripts/train_gmm_regime.py`` but refits
inside each walk-forward fold on daily market-level features (index volatility,
drawdown, breadth). The UK intraday ``models/gmm_regime_params.npz`` is not used.

* The scaler is fit on training rows only. ``regime_flags`` applies the training
  scaler to later rows and never refits.
* K is searched in {2, 3} by BIC. A fit with any component under the minimum
  weight is rejected. If no K survives the guard the model is degenerate and the
  filter stays invested (recorded, never silent).
* Components are ordered by a fixed risk score: mean standardised volatility
  minus mean standardised drawdown (drawdown is negative, so a deeper drawdown
  raises the score). The highest-risk component is the cash regime, so labels do
  not depend on the seed or the raw component index.
* Hysteresis: separate enter and exit thresholds on the cash posterior plus a
  minimum dwell, applied causally row by row.
* Each fold records K, seed and a model hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
from sklearn.mixture import GaussianMixture

from .params import RegimeSpec
from .signals import FeatureRow

K_CANDIDATES = (2, 3)
MIN_ROWS_PER_COMPONENT = 10
_DP = 8


def _matrix(rows: Sequence[FeatureRow]) -> np.ndarray:
    return np.array([[float(r.volatility), float(r.drawdown), float(r.breadth)] for r in rows], dtype=np.float64)


def _fmt(array: np.ndarray) -> Any:
    return [f"{value:.{_DP}f}" for value in np.asarray(array, dtype=np.float64).ravel().tolist()]


@dataclass
class RegimeModel:
    k: int
    seed: int
    degenerate: bool
    model_sha256: str
    scaler_mean: np.ndarray
    scaler_scale: np.ndarray
    gmm: GaussianMixture | None
    cash_component: int | None
    risk_order: tuple[int, ...]
    bic_by_k: dict[int, str]
    min_weight: str
    train_rows: int

    def record(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "seed": self.seed,
            "degenerate": self.degenerate,
            "model_sha256": self.model_sha256,
            "risk_order": list(self.risk_order),
            "bic_by_k": {str(key): value for key, value in sorted(self.bic_by_k.items())},
            "min_weight": self.min_weight,
            "train_rows": self.train_rows,
        }

    def cash_posterior(self, rows: Sequence[FeatureRow]) -> list[float]:
        if self.degenerate or self.gmm is None or self.cash_component is None or not rows:
            return [0.0] * len(rows)
        scaled = (_matrix(rows) - self.scaler_mean) / self.scaler_scale
        return self.gmm.predict_proba(scaled)[:, self.cash_component].tolist()


def _hash(k: int, seed: int, order: tuple[int, ...], mean: np.ndarray, scale: np.ndarray, gmm: GaussianMixture | None) -> str:
    body: dict[str, Any] = {"k": k, "seed": seed, "order": list(order), "scaler_mean": _fmt(mean), "scaler_scale": _fmt(scale)}
    if gmm is not None:
        body["weights"] = _fmt(gmm.weights_[list(order)])
        body["means"] = _fmt(gmm.means_[list(order)])
        body["covariances"] = _fmt(gmm.covariances_[list(order)])
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def fit_regime(train: Sequence[FeatureRow], *, spec: RegimeSpec, seed: int) -> RegimeModel:
    """Fit on training rows only."""
    x = _matrix(train)
    if len(x) == 0:
        raise ValueError("no training rows for the regime filter")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale = np.where(scale > 0.0, scale, 1.0)
    scaled = (x - mean) / scale
    guard = float(spec.min_component_weight)
    best: tuple[float, int, GaussianMixture] | None = None
    bic_by_k: dict[int, str] = {}
    min_weight_seen = 1.0
    for k in K_CANDIDATES:
        if len(scaled) < k * MIN_ROWS_PER_COMPONENT:
            continue
        gmm = GaussianMixture(
            n_components=k, covariance_type="full", reg_covar=float(spec.reg_covar), n_init=spec.n_init,
            random_state=seed, max_iter=300,
        )
        gmm.fit(scaled)
        bic = float(gmm.bic(scaled))
        bic_by_k[k] = f"{bic:.{_DP}f}"
        smallest = float(gmm.weights_.min())
        if smallest < guard:
            continue
        min_weight_seen = min(min_weight_seen, smallest)
        if best is None or bic < best[0]:
            best = (bic, k, gmm)
    if best is None:
        return RegimeModel(1, seed, True, _hash(1, seed, (), mean, scale, None), mean, scale, None, None, (), bic_by_k,
                           f"{0.0:.{_DP}f}", len(x))
    _, k, gmm = best
    risk = gmm.means_[:, 0] - gmm.means_[:, 1]  # standardised volatility minus standardised drawdown
    order = tuple(int(i) for i in np.argsort(risk, kind="stable"))
    return RegimeModel(k, seed, False, _hash(k, seed, order, mean, scale, gmm), mean, scale, gmm, order[-1], order,
                       bic_by_k, f"{float(gmm.weights_.min()):.{_DP}f}", len(x))


def regime_flags(model: RegimeModel, rows: Sequence[FeatureRow], spec: RegimeSpec) -> dict[date, bool]:
    """Causal cash flags (True = cash) with hysteresis. Row posteriors use past-only features."""
    posterior = model.cash_posterior(rows)
    enter, leave = float(spec.enter_cash), float(spec.exit_cash)
    cash = False
    dwell = spec.min_dwell
    flags: dict[date, bool] = {}
    for row, p in zip(rows, posterior):
        dwell += 1
        if not cash and p >= enter and dwell >= spec.min_dwell:
            cash, dwell = True, 0
        elif cash and p <= leave and dwell >= spec.min_dwell:
            cash, dwell = False, 0
        flags[row.session] = cash
    return flags
