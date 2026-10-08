"""Severity ordering of GMM regime components, and the one policy table that reads it.

A GMM component id carries no meaning. The shipped model happens to number its
components 0 normal, 1 crisis, 2 stressed, 3 calm, and an earlier hard-coded size
table assumed the ids were already in severity order, so the calmest component was
sized at 5% and the crisis component at 50%. The fix lives here:

* ``build_severity_map`` scores every component from the fitted model itself
  (``means[k, 0] + means[k, 1]``, training-standardized volatility plus spread) and
  derives one artifact-bound raw-id to severity-rank map. Rank 0 is the calmest.
* ``REGIME_POLICY_TABLE`` is the ONE place that says what each severity rank means:
  its label, its capital-scaling multiplier, its re-quote collar multiplier and its
  adapter slot. Every behavioural consumer reads it through the map.
* Raw ids stay in the model evidence. The map and the table are versioned and hashed
  (``policy_hash``) so an admission can prove which ordering and which sizes it used.

Nothing here reads a clock, the network or the filesystem. Anything that cannot be
ordered unambiguously raises ``RegimeSeverityError`` and the caller denies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import numpy as np

SEVERITY_MAPPING_VERSION = "gmm-severity-score-v1"
REGIME_POLICY_VERSION = "gmm-severity-policy-v1"

# Two components whose severity scores are this close are not ordered. They are never
# separated by raw id, because the raw id is arbitrary.
SCORE_TIE_TOLERANCE = 1e-9

_PARAM_KEYS = ("weights", "means", "precisions_cholesky", "scaler_mean", "scaler_var")
_WEIGHT_SUM_TOLERANCE = 1e-6
_MAX_PRECISION_CONDITION = 1e12


class RegimeSeverityError(RuntimeError):
    """A stable fail-closed refusal. Callers deny; none of these has a safe default."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RankPolicy:
    """What one severity rank means. Change a value here and nowhere else."""

    label: str
    size_multiplier: Decimal
    collar_multiplier: Decimal
    adapter_id: int


def _ranks(*rows: tuple[str, str, str, int]) -> tuple[RankPolicy, ...]:
    return tuple(RankPolicy(label, Decimal(size), Decimal(collar), adapter) for label, size, collar, adapter in rows)


# ONE named table, keyed by component count K (training searches K = 2..4) and then by
# severity rank (index 0 = calmest). Size values are the existing risk schedule
# (1.0, 0.5, 0.1, 0.05). The K = 2 and K = 3 rows are explicit and deliberately
# conservative: the most severe rank always keeps the smallest size. Collar values are
# the existing re-quote ladder (1, 1.5, 2); crisis does NOT widen it further (a wider BUY
# collar in the worst regime is the wrong direction), so crisis shares the widest value, 2.
# The operator can change any value here. Adapter slots keep the old clamp (ranks 0, 1, 2, 3 use adapters 0, 1, 2, 2).
REGIME_POLICY_TABLE: Mapping[int, tuple[RankPolicy, ...]] = MappingProxyType(
    {
        4: _ranks(
            ("calm", "1.0", "1.0", 0),
            ("normal", "0.5", "1.5", 1),
            ("stressed", "0.1", "2.0", 2),
            ("crisis", "0.05", "2.0", 2),
        ),
        3: _ranks(
            ("calm", "1.0", "1.0", 0),
            ("stressed", "0.1", "2.0", 1),
            ("crisis", "0.05", "2.0", 2),
        ),
        2: _ranks(
            ("calm", "1.0", "1.0", 0),
            ("stressed", "0.05", "2.0", 2),
        ),
    }
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def params_sha256(params: Mapping[str, Any]) -> str:
    """Content hash of the fitted arrays, independent of how they were serialized."""

    digest = hashlib.sha256()
    for key in _PARAM_KEYS:
        array = np.ascontiguousarray(np.asarray(params[key], dtype="<f8"))
        digest.update(key.encode("ascii"))
        digest.update(repr(array.shape).encode("ascii"))
        digest.update(array.tobytes())
    return digest.hexdigest()


@dataclass(frozen=True)
class RegimeSeverityMap:
    """Artifact-bound raw component id to severity rank, plus the policy read through it."""

    component_count: int
    scores: tuple[float, ...]
    rank_by_id: tuple[int, ...]
    id_by_rank: tuple[int, ...]
    params_sha256: str
    mapping_version: str
    policy_version: str
    policy_hash: str

    # --- lookups; every unknown id raises, none defaults -------------------------------

    def rank(self, raw_id: int) -> int:
        if not isinstance(raw_id, (int, np.integer)) or isinstance(raw_id, bool):
            raise RegimeSeverityError("REGIME_ID_UNKNOWN", "regime id must be an integer")
        if not 0 <= int(raw_id) < self.component_count:
            raise RegimeSeverityError("REGIME_ID_UNKNOWN", "regime id is not a component of this model")
        return self.rank_by_id[int(raw_id)]

    def _rank_policy(self, raw_id: int) -> RankPolicy:
        return REGIME_POLICY_TABLE[self.component_count][self.rank(raw_id)]

    def label(self, raw_id: int) -> str:
        return self._rank_policy(raw_id).label

    def size_multiplier(self, raw_id: int) -> float:
        return float(self._rank_policy(raw_id).size_multiplier)

    def collar_multiplier(self, raw_id: int) -> Decimal:
        return self._rank_policy(raw_id).collar_multiplier

    def adapter_id(self, raw_id: int, available: Sequence[int] | None = None) -> int:
        """Adapter slot for a component; clamped to the highest slot actually preloaded."""

        target = self._rank_policy(raw_id).adapter_id
        if available:
            return min(target, max(available))
        return target

    @property
    def calm_id(self) -> int:
        """Raw id of the calmest component (the synthetic id for local paper fixtures)."""

        return self.id_by_rank[0]

    def size_multipliers_by_id(self) -> dict[int, float]:
        return {raw: self.size_multiplier(raw) for raw in range(self.component_count)}

    def collar_multipliers_by_id(self) -> dict[int, Decimal]:
        return {raw: self.collar_multiplier(raw) for raw in range(self.component_count)}

    def audit(self, raw_id: int | None = None) -> dict[str, Any]:
        """JSON-safe evidence of the ordering and policy. Raw ids are kept as they are."""

        payload: dict[str, Any] = {
            "mapping_version": self.mapping_version,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "params_sha256": self.params_sha256,
            "component_count": self.component_count,
            "rank_by_id": list(self.rank_by_id),
            "scores": [repr(score) for score in self.scores],
        }
        if raw_id is not None:
            payload["regime_id"] = int(raw_id)
            payload["severity_rank"] = self.rank(raw_id)
            payload["severity_label"] = self.label(raw_id)
            payload["size_multiplier"] = str(REGIME_POLICY_TABLE[self.component_count][self.rank(raw_id)].size_multiplier)
        return payload


def _policy_hash(component_count: int, rank_by_id: Sequence[int], digest: str) -> str:
    document = {
        "mapping_version": SEVERITY_MAPPING_VERSION,
        "policy_version": REGIME_POLICY_VERSION,
        "params_sha256": digest,
        "component_count": component_count,
        "rank_by_id": list(rank_by_id),
        "policy": [
            [row.label, str(row.size_multiplier), str(row.collar_multiplier), row.adapter_id]
            for row in REGIME_POLICY_TABLE[component_count]
        ],
    }
    return hashlib.sha256(_canonical(document).encode("utf-8")).hexdigest()


def _array(params: Mapping[str, Any], key: str, code: str) -> np.ndarray:
    try:
        array = np.asarray(params[key], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegimeSeverityError(code, f"regime model parameter {key!r} is unusable") from exc
    if not np.isfinite(array).all():
        raise RegimeSeverityError(code, f"regime model parameter {key!r} is not finite")
    return array


def build_severity_map(params: Mapping[str, Any]) -> RegimeSeverityMap:
    """Order the model's components by severity, or raise. Never guesses.

    ``params`` are the fitted arrays (weights, means, precisions_cholesky, scaler_mean,
    scaler_var) in the order the predictor uses. The returned map is bound to them by
    content hash, so it cannot be reused for a different artifact.
    """

    weights = _array(params, "weights", "REGIME_MODEL_INVALID")
    if weights.ndim != 1 or weights.size == 0:
        raise RegimeSeverityError("REGIME_MODEL_INVALID", "regime weights must be a non-empty vector")
    count = int(weights.size)
    if count not in REGIME_POLICY_TABLE:
        raise RegimeSeverityError("REGIME_K_UNSUPPORTED", f"no regime policy is configured for {count} components")
    if np.any(weights <= 0.0) or abs(float(weights.sum()) - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise RegimeSeverityError("REGIME_MODEL_INVALID", "regime weights must be positive and sum to one")

    means = _array(params, "means", "REGIME_MODEL_INVALID")
    if means.shape != (count, 2):
        raise RegimeSeverityError("REGIME_MODEL_INVALID", "regime means have the wrong shape")

    scaler_mean = _array(params, "scaler_mean", "REGIME_SCALER_INVALID")
    scaler_var = _array(params, "scaler_var", "REGIME_SCALER_INVALID")
    if scaler_mean.shape != (2,) or scaler_var.shape != (2,) or np.any(scaler_var <= 0.0):
        raise RegimeSeverityError("REGIME_SCALER_INVALID", "regime scaler is invalid")

    cholesky = _array(params, "precisions_cholesky", "REGIME_COVARIANCE_INVALID")
    if cholesky.shape != (count, 2, 2):
        raise RegimeSeverityError("REGIME_COVARIANCE_INVALID", "regime precision factors have the wrong shape")
    for factor in cholesky:
        # The training pipeline (sklearn) writes the precision Cholesky factor UPPER
        # triangular with a strictly positive diagonal, and the predictor reads it that way
        # (y = diff @ factor, log-determinant from the diagonal). A negated, lower-triangular
        # or otherwise reshaped factor passes a positive-definiteness check (L @ L.T is
        # sign-blind) and then yields NaN posteriors, so the structure is checked itself.
        if np.any(np.tril(factor, -1) != 0.0) or np.any(np.diag(factor) <= 0.0):
            raise RegimeSeverityError(
                "REGIME_COVARIANCE_INVALID",
                "a regime precision factor is not upper triangular with a positive diagonal",
            )
        precision = factor @ factor.T
        try:
            eigenvalues = np.linalg.eigvalsh(precision)
            covariance = np.linalg.inv(precision)
        except np.linalg.LinAlgError as exc:
            raise RegimeSeverityError("REGIME_COVARIANCE_INVALID", "a regime covariance is singular") from exc
        if (
            not np.isfinite(covariance).all()
            or eigenvalues[0] <= 0.0
            or eigenvalues[-1] / eigenvalues[0] > _MAX_PRECISION_CONDITION
        ):
            raise RegimeSeverityError("REGIME_COVARIANCE_INVALID", "a regime covariance is not positive definite")

    scores = means[:, 0] + means[:, 1]
    order = np.argsort(scores, kind="stable")
    ordered = scores[order]
    if np.any(np.diff(ordered) <= SCORE_TIE_TOLERANCE):
        raise RegimeSeverityError("REGIME_SEVERITY_AMBIGUOUS", "two regime components have tied severity scores")
    rank_by_id = [0] * count
    for rank, raw in enumerate(order.tolist()):
        rank_by_id[raw] = rank

    digest = params_sha256(params)
    return RegimeSeverityMap(
        component_count=count,
        scores=tuple(float(score) for score in scores),
        rank_by_id=tuple(rank_by_id),
        id_by_rank=tuple(int(raw) for raw in order),
        params_sha256=digest,
        mapping_version=SEVERITY_MAPPING_VERSION,
        policy_version=REGIME_POLICY_VERSION,
        policy_hash=_policy_hash(count, rank_by_id, digest),
    )


# --- the SQL policy the risk gate reads -----------------------------------------------


def build_scaling_policy_connection(severity_map: RegimeSeverityMap) -> sqlite3.Connection:
    """In-memory policy tables derived from one map.

    ``scaling_policies.regime_id`` is the RAW model component id (what the classifier
    emits and the audit records); its multiplier comes from the component's severity
    rank. ``scaling_policy_meta`` carries the policy hash so the gate can refuse a
    table that was not built from the map the caller classified with.
    """

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)"
    )
    connection.execute("CREATE TABLE scaling_policy_meta (policy_hash TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO scaling_policies (regime_id, scale_multiplier) VALUES (?, ?)",
        sorted(severity_map.size_multipliers_by_id().items()),
    )
    connection.execute(
        "INSERT INTO scaling_policy_meta (policy_hash) VALUES (?)", (severity_map.policy_hash,)
    )
    return connection


def policy_table_matches(connection: Any, severity_map: RegimeSeverityMap) -> bool:
    """True only when the connection's ACTUAL rows equal the trusted map's policy.

    The expected multipliers are recomputed from the trusted map's rank order and
    ``REGIME_POLICY_TABLE``. Nothing stored in the connection is trusted: not its hash row
    (which is only checked to agree) and not any row, so a table whose crisis multiplier was
    raised from 0.05 to 1.0 with the metadata left alone is refused, as is a table with a
    missing, extra or renumbered row.
    """

    try:
        recomputed = _policy_hash(
            severity_map.component_count, severity_map.rank_by_id, severity_map.params_sha256
        )
        if recomputed != severity_map.policy_hash:
            return False  # the map no longer describes the table it was built with
        expected = severity_map.size_multipliers_by_id()
        cursor = connection.cursor()
        cursor.execute("SELECT regime_id, scale_multiplier FROM scaling_policies")
        rows = cursor.fetchall()
        actual = {int(row[0]): float(row[1]) for row in rows}
        if len(rows) != len(actual) or actual != expected:
            return False
        cursor.execute("SELECT policy_hash FROM scaling_policy_meta")
        meta = cursor.fetchall()
    except Exception:  # noqa: BLE001 - a missing table or a broken connection is a mismatch
        return False
    return len(meta) == 1 and meta[0][0] == severity_map.policy_hash


def validate_posterior(probabilities: Any, component_count: int) -> np.ndarray:
    """A posterior is usable only if it is a finite probability vector over every component.

    ``argmax`` of a NaN vector is index 0, which would silently pick whichever component
    is numbered 0. Every place that takes an ``argmax`` calls this first.
    """

    try:
        vector = np.asarray(probabilities, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise RegimeSeverityError("REGIME_POSTERIOR_INVALID", "regime posterior is not numeric") from exc
    if (
        vector.shape != (component_count,)
        or not np.isfinite(vector).all()
        or np.any(vector < 0.0)
        or np.any(vector > 1.0 + 1e-9)
        or abs(float(vector.sum()) - 1.0) > 1e-6
    ):
        raise RegimeSeverityError("REGIME_POSTERIOR_INVALID", "regime posterior is not a probability vector")
    return vector
