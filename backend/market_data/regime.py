"""Fail-closed runtime Phase 50 regime classification for market snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import numpy as np

from coreml.fast_gmm import fast_gmm_predict_proba
from coreml.gmm_loader import load_gmm_params

from .admission import RegimeEvidence
from .models import IndiaInstrument
from .session import MarketDataError, MarketDataSession


_REQUIRED = ("weights", "means", "precisions_cholesky", "scaler_mean", "scaler_var")


class RegimeClassifier:
    """Read-only GMM artifact adapter with snapshot-bound evidence output."""

    def __init__(self, artifact_path: str | Path | None = None) -> None:
        self._artifact_path = Path(artifact_path or Path(__file__).resolve().parents[2] / "models" / "gmm_regime_params.npz")
        try:
            raw = self._artifact_path.read_bytes()
            self._params = load_gmm_params(str(self._artifact_path))
        except FileNotFoundError as exc:
            raise MarketDataError("REGIME_MODEL_UNAVAILABLE", "regime model is unavailable") from exc
        except (OSError, ValueError) as exc:
            raise MarketDataError("REGIME_MODEL_INVALID", "regime model is invalid") from exc
        self._validate_params()
        self.model_version = f"gmm-p256:{sha256(raw).hexdigest()}"

    def evidence(
        self,
        session: MarketDataSession,
        instrument: IndiaInstrument,
        *,
        now: datetime | None = None,
    ) -> RegimeEvidence:
        checked_now = now or datetime.now(timezone.utc)
        snapshot = session.snapshot(instrument, now=checked_now)
        window = session.tick_window(instrument, now=checked_now)
        bids, asks = window["bid"], window["ask"]
        if len(bids) < 3 or len(asks) < 3:
            raise MarketDataError("REGIME_WINDOW_INSUFFICIENT", "regime classification requires at least three quotes")
        mids = (np.asarray(bids, dtype=np.float64) + np.asarray(asks, dtype=np.float64)) / 2.0
        if np.any(mids <= 0.0):
            raise MarketDataError("REGIME_FEATURE_INVALID", "regime midpoint must be positive")
        returns = np.diff(np.log(mids))
        volatility = float(np.std(returns))
        spread = float(snapshot.spread_pct)
        feature = np.asarray((volatility, spread), dtype=np.float64)
        if not np.isfinite(feature).all() or np.any(feature < 0.0):
            raise MarketDataError("REGIME_FEATURE_INVALID", "regime features are invalid")
        try:
            probabilities = fast_gmm_predict_proba(feature, **self._params)
        except Exception as exc:
            raise MarketDataError("REGIME_INFERENCE_FAILED", "regime inference failed") from exc
        if not np.isfinite(probabilities).all() or probabilities.ndim != 1:
            raise MarketDataError("REGIME_INFERENCE_FAILED", "regime inference produced invalid output")
        return RegimeEvidence(
            instrument=instrument,
            regime_id=int(np.argmax(probabilities)),
            observed_at=snapshot.quote_observed_at,
            model_version=self.model_version,
            source_snapshot_id=snapshot.snapshot_id,
        )

    def _validate_params(self) -> None:
        if tuple(self._params) != _REQUIRED:
            raise MarketDataError("REGIME_MODEL_INVALID", "regime model keys are invalid")
        weights = self._params["weights"]
        means = self._params["means"]
        precisions = self._params["precisions_cholesky"]
        scaler_mean = self._params["scaler_mean"]
        scaler_var = self._params["scaler_var"]
        if (
            weights.ndim != 1
            or means.shape != (len(weights), 2)
            or precisions.shape != (len(weights), 2, 2)
            or scaler_mean.shape != (2,)
            or scaler_var.shape != (2,)
            or not all(np.isfinite(value).all() for value in self._params.values())
            or np.any(weights <= 0.0)
            or np.any(scaler_var <= 0.0)
        ):
            raise MarketDataError("REGIME_MODEL_INVALID", "regime model shape or values are invalid")
