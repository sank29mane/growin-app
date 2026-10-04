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

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
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
    """UK needs only a manifest until Phase 66."""

    model_config = _STRICT_MODEL

    schema_version: SchemaVersion
    workspace: Literal["uk"]
    currency: Literal["GBP"]
