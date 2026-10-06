"""Purged, forward-only walk-forward folds (D-13).

* Folds are chronological and expanding. Training always ends before the test
  window starts and nothing after a test window is used to train.
* Purging uses real outcome-availability dates. A training observation whose
  trade outcome is not fully known by the fitting cutoff is dropped, so an exit
  delayed by ``NO_ASSUMED_FILL`` (or a locked band) becomes available only at its
  real fill.
* The same purge runs at the holdout boundary: no development observation whose
  outcome resolves on or after the holdout start is used.
* No fixed gap is used. If one is ever needed it is sized from the maximum
  forward outcome horizon, never from feature lookback plus holding period.
* Features may warm up on earlier prices: only fitting rows are purged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .errors import StrategyIndiaError


@dataclass(frozen=True)
class FoldRules:
    """Registered split rules (D-10). ``as_payload`` is what the registry stores."""

    n_folds: int
    test_sessions: int
    min_train_sessions: int
    scheme: str = "expanding"

    def __post_init__(self) -> None:
        if self.scheme != "expanding":
            raise StrategyIndiaError("only the expanding forward-only scheme is implemented")
        for name in ("n_folds", "test_sessions", "min_train_sessions"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise StrategyIndiaError(f"{name} must be a positive int")

    def as_payload(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "n_folds": self.n_folds,
            "test_sessions": self.test_sessions,
            "min_train_sessions": self.min_train_sessions,
            "purge": "outcome_availability",
            "gap_sessions": 0,
            "embargo_sessions": 0,
        }


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: date
    cutoff: date  # last training session
    test_start: date
    test_end: date

    def in_test(self, day: date) -> bool:
        return self.test_start <= day <= self.test_end


def make_folds(dev_sessions: Sequence[date], rules: FoldRules) -> tuple[Fold, ...]:
    """Contiguous test windows counted back from the last development session."""
    sessions = sorted(set(dev_sessions))
    total_test = rules.n_folds * rules.test_sessions
    if len(sessions) < rules.min_train_sessions + total_test:
        raise StrategyIndiaError(
            f"{len(sessions)} development sessions cannot hold {rules.n_folds} folds of {rules.test_sessions} "
            f"after {rules.min_train_sessions} training sessions"
        )
    first_test = len(sessions) - total_test
    folds: list[Fold] = []
    for k in range(rules.n_folds):
        start = first_test + k * rules.test_sessions
        end = start + rules.test_sessions - 1
        fold = Fold(k + 1, sessions[0], sessions[start - 1], sessions[start], sessions[end])
        if not fold.cutoff < fold.test_start:
            raise StrategyIndiaError("a fold trains on a session at or after its test start")
        folds.append(fold)
    for earlier, later in zip(folds, folds[1:]):
        if not earlier.test_end < later.test_start:
            raise StrategyIndiaError("fold test windows overlap")
    return tuple(folds)


@dataclass(frozen=True)
class Observation:
    """One training observation: a trade and the date its outcome became known."""

    entry_date: date
    outcome_date: date | None  # real final-fill date; None while unresolved
    score: Decimal
    net_return: Decimal


def purge_at_cutoff(observations: Sequence[Observation], cutoff: date) -> tuple[list[Observation], list[Observation]]:
    """Split into (kept, purged). Kept: entered by the cutoff and fully resolved by it."""
    kept: list[Observation] = []
    purged: list[Observation] = []
    for obs in observations:
        if obs.entry_date > cutoff:
            continue  # not a training observation of this fold at all
        if obs.outcome_date is None or obs.outcome_date > cutoff:
            purged.append(obs)
        else:
            kept.append(obs)
    return kept, purged


def purge_at_holdout(observations: Sequence[Observation], holdout_start: date) -> tuple[list[Observation], list[Observation]]:
    """Drop development observations whose outcome is unresolved or resolves inside the holdout."""
    kept: list[Observation] = []
    purged: list[Observation] = []
    for obs in observations:
        if obs.outcome_date is None or obs.outcome_date >= holdout_start:
            purged.append(obs)
        else:
            kept.append(obs)
    return kept, purged
