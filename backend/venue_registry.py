"""The declarative venue registry: one ``VenueSpec`` per bound venue kind.

A venue kind is bound to one workspace, one currency and a closed set of order
modes. Every guard (the order-mode rule, the ledger open checks, the ledger DDL,
the private-config loader and the dispatcher lookup) reads its facts from
the sealed registry through the functions below. Adding a venue means adding
one spec here and one dispatcher factory; no guard function changes.

The registry is built and validated once at import and held privately. The
public ``VENUE_SPECS`` name is a read-only view of it: reassigning that module
attribute at runtime changes nothing the readers see. Tests that need another
spec use ``override_venue_specs``, which validates its input and restores the
sealed registry on exit.

This module imports only the standard library, so the execution package and the
private-config package can both read it without importing each other. Import it
flat (``import venue_registry``) from both so there is exactly one registry.

``paper`` is not a spec: it is the unbound ledger and has no account to bind.
LIVE is not an allowed mode of any spec: the constructor refuses it.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, Optional

VENUE_PAPER = "paper"
VENUE_T212_PRACTICE = "t212_practice"

KNOWN_WORKSPACES = frozenset({"uk", "india"})
LIVE_MODE = "LIVE"
# The non-LIVE order modes a spec may allow: ``execution.models.OrderMode``
# minus LIVE. This module cannot import it, so a test pins the two together.
KNOWN_ORDER_MODES = frozenset({"PAPER", "PRACTICE"})
# The currency each workspace may bind a venue in.
WORKSPACE_CURRENCIES: Mapping[str, str] = MappingProxyType({"uk": "GBP", "india": "INR"})

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
        if not modes <= KNOWN_ORDER_MODES:
            raise ValueError("a venue spec names only known order modes")
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

def _validated_copy(kind: object, spec: object) -> VenueSpec:
    """A fresh, fully re-validated copy of one registry entry, or ValueError.

    Re-running the constructor on a copy means a spec whose fields were forced
    after construction cannot enter the registry, and nothing the caller still
    holds can change the sealed entry later.
    """

    if type(spec) is not VenueSpec:
        raise ValueError("a registry entry is a VenueSpec")
    if not isinstance(kind, str) or spec.kind != kind:
        raise ValueError("a registry key is its spec's kind")
    copy = VenueSpec(
        kind=spec.kind,
        workspace=spec.workspace,
        currency=spec.currency,
        modes=spec.modes,
        ledger_path=spec.ledger_path,
        dispatcher_key=spec.dispatcher_key,
    )
    if WORKSPACE_CURRENCIES.get(copy.workspace) != copy.currency:
        raise ValueError("a venue spec's currency is its workspace's currency")
    return copy


def _seal(entries: Mapping[object, object]) -> Mapping[str, VenueSpec]:
    """Validate every entry and return a read-only mapping of fresh copies."""

    if not isinstance(entries, Mapping):
        raise ValueError("a registry is a mapping of kind to VenueSpec")
    sealed = {}
    for kind, spec in entries.items():
        copy = _validated_copy(kind, spec)
        sealed[copy.kind] = copy
    return MappingProxyType(sealed)


class _Registry:
    """The one private holder every reader goes through."""

    __slots__ = ("specs",)

    def __init__(self, specs: Mapping[str, VenueSpec]) -> None:
        self.specs = specs


class _SpecsView(Mapping):
    """A read-only live view of the sealed registry, kept for readers of the old name."""

    __slots__ = ()

    def __getitem__(self, kind: str) -> VenueSpec:
        return _REGISTRY.specs[kind]

    def __iter__(self) -> Iterator[str]:
        return iter(_REGISTRY.specs)

    def __len__(self) -> int:
        return len(_REGISTRY.specs)


# Built and validated once, at import. Everything below reads _REGISTRY.specs,
# never a public name, so reassigning VENUE_SPECS or T212_PRACTICE_SPEC at
# runtime cannot register, replace or remove a venue.
_REGISTRY = _Registry(_seal({T212_PRACTICE_SPEC.kind: T212_PRACTICE_SPEC}))
VENUE_SPECS: Mapping[str, VenueSpec] = _SpecsView()


@contextmanager
def override_venue_specs(extra: Mapping[str, VenueSpec]) -> Iterator[None]:
    """Test seam: add or replace specs for the duration of a ``with`` block.

    ``extra`` is merged over the registry in force and the merge is validated
    exactly as the import-time registry was. A malformed entry raises
    ``ValueError`` before anything changes. The previous registry is restored on
    exit, however the block ends. Not for production code.
    """

    candidate = _seal({**_REGISTRY.specs, **dict(extra)})
    previous = _REGISTRY.specs
    _REGISTRY.specs = candidate
    try:
        yield
    finally:
        _REGISTRY.specs = previous


def spec_for(kind: object) -> Optional[VenueSpec]:
    """The spec registered for ``kind``, or None. None is always a refusal."""

    if not isinstance(kind, str):
        return None
    return _REGISTRY.specs.get(kind)


def registered_kinds() -> tuple[str, ...]:
    """The bound venue kinds, sorted. ``paper`` is not among them."""

    return tuple(sorted(_REGISTRY.specs))


def known_venues() -> tuple[str, ...]:
    """``paper`` followed by every registered kind."""

    return (VENUE_PAPER, *registered_kinds())


__all__ = [
    "KNOWN_ORDER_MODES",
    "KNOWN_WORKSPACES",
    "LIVE_MODE",
    "T212_PRACTICE_SPEC",
    "VENUE_PAPER",
    "VENUE_SPECS",
    "VENUE_T212_PRACTICE",
    "WORKSPACE_CURRENCIES",
    "VenueSpec",
    "known_venues",
    "override_venue_specs",
    "registered_kinds",
    "spec_for",
    "venue_ledger_path",
]
