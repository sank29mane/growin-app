"""Errors for the model role registry.

A registry error carries a stable code and a field path. It never carries a
value read from ``models.json``: the file names endpoints and key variables
and none of that belongs in logs, chat transcripts or crash reports.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

# Field paths come from the file (provider ids, unknown key names). Only a
# conservative alphabet survives, and only a short prefix, so a hostile file
# cannot smuggle a value into an error through a key name.
_FIELD_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")
_FIELD_MAX = 64


def safe_field(field: str) -> str:
    return _FIELD_SAFE.sub("_", str(field))[:_FIELD_MAX]


class ModelRegistryError(Exception):
    """Raised for every malformed, missing or unusable registry input."""

    def __init__(self, code: str, field: str = "") -> None:
        super().__init__(code, safe_field(field))
        self.code = code
        self.field = safe_field(field)

    def __str__(self) -> str:
        return f"{self.code}: {self.field}"


class ModelRoleMissing(ModelRegistryError):
    """The loaded registry has no entry for the requested role."""

    def __init__(self, role: str) -> None:
        super().__init__("ROLE_MISSING", role)
        self.role = self.field


class ModelRegistryUnavailable(ModelRegistryError):
    """No valid registry is loaded. AI routes answer 503 with this code."""

    def __init__(self, reason: str = "") -> None:
        super().__init__("MODEL_REGISTRY_UNAVAILABLE", reason or "registry")
        self.reason = reason


class ProviderError(Exception):
    """A provider call failed. ``code`` is stable, ``role`` names the caller.

    The message never includes a response body, a URL or a key.
    """

    def __init__(self, code: str, role: str) -> None:
        super().__init__(code, safe_field(role))
        self.code = code
        self.role = safe_field(role)

    def __str__(self) -> str:
        return f"{self.code}: {self.role}"


def schema_error_from_validation_error(exc: ValidationError) -> ModelRegistryError:
    """Map the first pydantic error to a code using ``type`` and ``loc`` only.

    Pydantic puts the rejected value into each error's ``input``, ``msg`` and
    ``ctx``. None of those are read here.
    """

    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        error_type = error.get("type", "")
        if error_type == "extra_forbidden":
            return ModelRegistryError("UNKNOWN_FIELD", location)
        if error_type == "literal_error" and location.endswith("kind"):
            return ModelRegistryError("UNKNOWN_KIND", location)
        return ModelRegistryError("FIELD_INVALID", location)
    return ModelRegistryError("FIELD_INVALID", "")
