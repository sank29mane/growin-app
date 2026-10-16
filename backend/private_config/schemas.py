"""Strict schemas for ``private/<workspace>/`` files.

Every model forbids unknown keys and is frozen. Money and limit fields are
decimal strings: a JSON number is refused because it would pass through a
binary float on the way in. There is no default for any field, so a missing
key is a validation failure and never a silently supplied value.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal

from venue_registry import known_venues, spec_for

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

DECIMAL_STRING_PATTERN = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"
_DECIMAL_STRING_RE = re.compile(DECIMAL_STRING_PATTERN)

SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"
Sha256Hex = Annotated[str, StringConstraints(pattern=SHA256_HEX_PATTERN)]


def _require_decimal_string(value: Any) -> str:
    # Pydantic 2.12.5 strict mode alone still accepts JSON numbers for Decimal
    # fields, so this validator is what enforces "string only".
    if not isinstance(value, str):
        raise ValueError("decimal field must be a string")
    if _DECIMAL_STRING_RE.fullmatch(value) is None:
        raise ValueError("decimal field is malformed")
    return value


DecimalStr = Annotated[
    Decimal,
    BeforeValidator(_require_decimal_string),
    Field(allow_inf_nan=False),
]


def _require_plain_int(value: Any) -> Any:
    # Lax mode turns JSON true into 1 before the Literal check, so Literal[1]
    # alone would accept it.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("schema_version must be an integer")
    return value


SchemaVersion = Annotated[Literal[1], BeforeValidator(_require_plain_int)]

_STRICT_MODEL = ConfigDict(extra="forbid", frozen=True)


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("floats are not allowed in params")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


class FileRef(BaseModel):
    """Reference to a research or holdout file: relative path plus sha256."""

    model_config = _STRICT_MODEL

    path: str
    sha256: Sha256Hex


class IndiaLimits(BaseModel):
    """India pilot limits. All amounts and fractions are decimal strings."""

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["india"]
    currency: Literal["INR"]
    capital_cap: DecimalStr = Field(repr=False)
    per_position_cap: DecimalStr = Field(repr=False)
    drawdown_halt: DecimalStr = Field(repr=False)
    drawdown_flatten: DecimalStr = Field(repr=False)
    position_stop: DecimalStr = Field(repr=False)


class IndiaStrategy(BaseModel):
    """India strategy parameters with a version, a hash and result references."""

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["india"]
    strategy_params_version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    params: dict[str, Any] = Field(repr=False)
    params_sha256: Sha256Hex
    research_refs: list[FileRef]
    holdout_refs: list[FileRef]

    @field_validator("params")
    @classmethod
    def _params_non_empty_and_float_free(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("params must not be empty")
        _reject_floats(value)
        return value


class UkManifest(BaseModel):
    """UK paper needs only a manifest. A practice venue also needs execution and limits."""

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["uk"]
    currency: Literal["GBP"]


ACCOUNT_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class WorkspaceExecution(BaseModel):
    """``private/<workspace>/execution.json``: which venue this workspace trades on.

    ``paper`` carries no account. A bound venue (one with a ``VenueSpec`` in
    ``venue_registry``) names the broker account id and the currency its ledger
    is bound to, which must be the spec's currency. The venue is a plain string
    here and the loader turns an unknown one into VENUE_UNKNOWN before this runs.
    """

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["uk", "india"]
    venue: str
    # These two are the only fields with a default: None means "absent", and the
    # validator below decides per venue whether absence is allowed.
    account_id: Annotated[str, StringConstraints(pattern=ACCOUNT_ID_PATTERN)] | None = Field(
        default=None, repr=False
    )
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    # Phase 66 D-26: the UK practice slippage cap in basis points (proposed 25, the
    # operator may change it). It is kept exactly as written (no validation here)
    # because a missing, null, non-numeric, zero or negative value must make
    # admission DENY every order, never block start-up and never mean "no cap".
    # The reader is ``market_data.admission.parse_max_slippage_bps``. A paper
    # venue ignores the field. JSON floats are already refused by the loader.
    max_slippage_bps: Any = Field(default=None, repr=False)
    # Phase 63 (P-15): the India fat-finger collar. Kept raw here for the same reason as
    # ``max_slippage_bps``: the 62 research load and any non-execution read must not fail on
    # an India execution value. ``IndiaExecution`` (the execution start path) validates it.
    # UK has no collar, so a UK file naming one is refused.
    fat_finger_collar: Any = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _venue_shape(self) -> "WorkspaceExecution":
        if self.workspace == "uk" and self.fat_finger_collar is not None:
            raise ValueError("fat_finger_collar is an India field")
        if self.venue not in known_venues():
            raise ValueError("venue is unknown")
        spec = spec_for(self.venue)
        if spec is not None:
            if self.account_id is None or self.currency is None:
                raise ValueError("a bound venue needs account_id and currency")
            if self.currency != spec.currency:
                raise ValueError("currency is not the venue's currency")
        elif {"account_id", "currency"} & self.model_fields_set:
            raise ValueError("the paper venue takes no account_id or currency")
        return self


class IndiaExecution(WorkspaceExecution):
    """``private/india/execution.json`` for India execution authority (Phase 63, P-15, D-09).

    The same file as ``WorkspaceExecution`` (venue and optional bound-venue fields), plus the
    two values the Mac's own India limits need that ``limits.json`` does not carry: the
    fat-finger collar and ``max_slippage_bps``. Both are required decimal strings; a float,
    an unknown key, a collar outside (0, 1) or a non-positive slippage cap is refused. The
    values live in ``private/`` alone and are hidden from ``repr``.

    Only the execution start path loads this model. The 62 research load reads the file as
    ``WorkspaceExecution`` (values unchecked), so 62's sealed inputs do not change.
    """

    workspace: Literal["india"]
    fat_finger_collar: DecimalStr = Field(repr=False)
    max_slippage_bps: DecimalStr = Field(repr=False)

    @field_validator("fat_finger_collar")
    @classmethod
    def _collar_in_open_unit_interval(cls, value: Decimal) -> Decimal:
        if not Decimal(0) < value < Decimal(1):
            raise ValueError("fat_finger_collar must lie between 0 and 1")
        return value

    @field_validator("max_slippage_bps")
    @classmethod
    def _slippage_positive(cls, value: Decimal) -> Decimal:
        if not value > Decimal(0):
            raise ValueError("max_slippage_bps must be positive")
        return value


class UkLimits(BaseModel):
    """UK practice caps. Decimal strings only; the values live in private/ alone."""

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["uk"]
    currency: Literal["GBP"]
    capital_cap: DecimalStr = Field(repr=False)
    per_position_cap: DecimalStr = Field(repr=False)
