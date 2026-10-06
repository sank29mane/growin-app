"""The declarative venue registry: one ``VenueSpec`` per bound venue kind.

A venue kind is bound to one workspace, one currency and a closed set of order
modes. Every guard (the order-mode rule, the ledger open checks, the ledger DDL,
the private-config loader and the dispatcher lookup) reads its facts from
``VENUE_SPECS`` through the functions below. Adding a venue means adding one
spec here and one dispatcher factory; no guard function changes.

This module imports only the standard library, so the execution package and the
private-config package can both read it without importing each other. Import it
flat (``import venue_registry``) from both so there is exactly one registry.

``paper`` is not a spec: it is the unbound ledger and has no account to bind.
LIVE is not an allowed mode of any spec: the constructor refuses it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional

VENUE_PAPER = "paper"
VENUE_T212_PRACTICE = "t212_practice"

KNOWN_WORKSPACES = frozenset({"uk", "india"})
LIVE_MODE = "LIVE"

_KIND_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_MODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class VenueSpec:
    """Everything the guards need to know about one bound venue kind."""

    kind: str
    workspace: str
    currency: str
    modes: frozenset[str]
    ledger_path: Callable[[], Path]
    dispatcher_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or _KIND_RE.fullmatch(self.kind) is None:
            raise ValueError("a venue kind is lower-case words joined by underscores")
        if self.kind == VENUE_PAPER:
            raise ValueError("paper is the unbound ledger and has no spec")
        if self.workspace not in KNOWN_WORKSPACES:
            raise ValueError("a venue spec names a known workspace")
        if not isinstance(self.currency, str) or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise ValueError("a venue spec names a three-letter currency")
        modes = frozenset(self.modes)
        if not modes or any(
            not isinstance(mode, str) or _MODE_RE.fullmatch(mode) is None for mode in modes
        ):
            raise ValueError("a venue spec names at least one upper-case order mode")
        if LIVE_MODE in modes:
            raise ValueError("no venue spec may allow LIVE")
        object.__setattr__(self, "modes", modes)
        if not callable(self.ledger_path):
            raise ValueError("a venue spec needs a default ledger path function")
        if not isinstance(self.dispatcher_key, str) or _KIND_RE.fullmatch(self.dispatcher_key) is None:
            raise ValueError("a venue spec needs a dispatcher factory key")


def venue_ledger_path(workspace: str, kind: str) -> Path:
    """The default local ledger path of a bound venue, never the workspace's real ledger."""

    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Growin"
        / "workspaces"
        / f"{workspace}-{kind.replace('_', '-')}"
        / "execution.sqlite3"
    )


def _t212_practice_ledger_path() -> Path:
    return venue_ledger_path("uk", VENUE_T212_PRACTICE)


T212_PRACTICE_SPEC = VenueSpec(
    kind=VENUE_T212_PRACTICE,
    workspace="uk",
    currency="GBP",
    modes=frozenset({"PRACTICE"}),
    ledger_path=_t212_practice_ledger_path,
    dispatcher_key=VENUE_T212_PRACTICE,
)

# The production registry. A test replaces this name to exercise another spec;
# every reader below looks it up at call time.
VENUE_SPECS: Mapping[str, VenueSpec] = MappingProxyType(
    {T212_PRACTICE_SPEC.kind: T212_PRACTICE_SPEC}
)


def spec_for(kind: object) -> Optional[VenueSpec]:
    """The spec registered for ``kind``, or None. None is always a refusal."""

    if not isinstance(kind, str):
        return None
    return VENUE_SPECS.get(kind)


def registered_kinds() -> tuple[str, ...]:
    """The bound venue kinds, sorted. ``paper`` is not among them."""

    return tuple(sorted(VENUE_SPECS))


def known_venues() -> tuple[str, ...]:
    """``paper`` followed by every registered kind."""

    return (VENUE_PAPER, *registered_kinds())


__all__ = [
    "KNOWN_WORKSPACES",
    "LIVE_MODE",
    "T212_PRACTICE_SPEC",
    "VENUE_PAPER",
    "VENUE_SPECS",
    "VENUE_T212_PRACTICE",
    "VenueSpec",
    "known_venues",
    "registered_kinds",
    "spec_for",
    "venue_ledger_path",
]
