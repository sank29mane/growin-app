"""Errors for the private configuration loader.

An error carries a stable code and a field name. It never carries an input
value: the values in ``private/`` are the trading edge and must not reach logs,
chat transcripts or crash reports.
"""

from __future__ import annotations

from pydantic import ValidationError


class PrivateConfigError(Exception):
    """Raised for every malformed or missing private configuration input."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(code, field)
        self.code = code
        self.field = field

    def __str__(self) -> str:
        return f"{self.code}: {self.field}"


def schema_error_from_validation_error(exc: ValidationError) -> PrivateConfigError:
    """Convert a pydantic error into SCHEMA_INVALID using ``loc`` only.

    Pydantic puts the rejected value into each error's ``input``, ``msg`` and
    ``ctx``. None of those are read here.
    """

    field = ""
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = error.get("loc", ())
        field = ".".join(str(part) for part in location)
        break
    return PrivateConfigError("SCHEMA_INVALID", field)
