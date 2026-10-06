"""Typed refusals. Every one fails closed and carries a stable code."""

from __future__ import annotations


class StrategyIndiaError(ValueError):
    """Base class for every refusal raised by backend/strategy_india."""

    code = "strategy_india_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class GateRefused(StrategyIndiaError):
    """The 59 band coverage gate (D-01, D-01a) refuses the run. CLI exit code 3."""

    code = "gate_refused"
    exit_code = 3


class RegistryError(StrategyIndiaError):
    """The registration record is missing, malformed, tampered with or refused."""

    code = "registry_error"


class RegistryMismatch(RegistryError):
    """A recorded value differs from the live input (D-10)."""

    code = "registry_mismatch"

    def __init__(self, field: str, message: str | None = None) -> None:
        super().__init__(message or f"registered {field} differs from the live input")
        self.field = field


class HoldoutViolation(StrategyIndiaError):
    """Research code asked for a session inside the sealed holdout (D-12)."""

    code = "holdout_violation"


class HoldoutSpent(HoldoutViolation):
    """The holdout was already opened; it is one shot (D-12, D-19)."""

    code = "holdout_spent"


class ParamsError(StrategyIndiaError):
    """Strategy parameters violate the schema or a locked rule."""

    code = "params_error"


class DataError(StrategyIndiaError):
    """A dataset row or an input series is unusable."""

    code = "data_error"


class HoldoutInvalid(HoldoutViolation):
    """The holdout was opened and then the evaluation failed. The spend is recorded in the registry, never silent."""

    code = "holdout_invalid"
