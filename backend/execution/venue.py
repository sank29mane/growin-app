"""The execution venue seam (Phase 66, E1).

A venue names where an order goes. ``paper`` acknowledges locally. A practice
venue is bound to one broker practice account and accepts only ``PRACTICE``
orders. This module holds the venue ids, the immutable ledger binding, the one
mode rule every gate shares, and the dispatcher factory map that
``AppState.start_execution`` selects from.

It imports only ``models`` so the ledger, approval and service layers can all
use it without a cycle. It contacts no broker, and the production map holds
only ``paper``: a practice dispatcher is registered by tests, or by a later
plan, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from .models import OrderAck, OrderIntent, OrderMode, Workspace

VENUE_PAPER = "paper"
VENUE_T212_PRACTICE = "t212_practice"
KNOWN_VENUES: tuple[str, ...] = (VENUE_PAPER, VENUE_T212_PRACTICE)

# The one place a venue maps to the order mode it accepts. LIVE is in no row.
VENUE_MODE: Mapping[str, OrderMode] = MappingProxyType(
    {VENUE_PAPER: OrderMode.PAPER, VENUE_T212_PRACTICE: OrderMode.PRACTICE}
)
PRACTICE_VENUES: frozenset[str] = frozenset({VENUE_T212_PRACTICE})
PRACTICE_CURRENCY = "GBP"
ACCOUNT_ID_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"
_ACCOUNT_ID_RE = re.compile(ACCOUNT_ID_PATTERN)

# Refusal codes shared by the service, approval and ledger gates.
LIVE_DISABLED = "LIVE_DISABLED"
MODE_VENUE_MISMATCH = "MODE_VENUE_MISMATCH"
BROKER_VENUE_MISMATCH = "BROKER_VENUE_MISMATCH"
ACCOUNT_BINDING_MISMATCH = "ACCOUNT_BINDING_MISMATCH"

_REFUSAL_TEXT = {
    LIVE_DISABLED: "live execution remains disabled",
    MODE_VENUE_MISMATCH: "order mode is not accepted by this ledger's venue",
    BROKER_VENUE_MISMATCH: "order broker does not match this ledger's venue",
    ACCOUNT_BINDING_MISMATCH: "order account does not match this ledger's venue binding",
}


class VenueError(RuntimeError):
    """A venue cannot be selected. Carries a stable code and the venue id only."""

    def __init__(self, code: str, venue: str = "") -> None:
        super().__init__(code, venue)
        self.code = code
        self.venue = venue

    def __str__(self) -> str:
        return f"{self.code}: {self.venue}" if self.venue else self.code


class VenueBinding(BaseModel):
    """What a practice ledger is bound to at creation. Never changes afterwards."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    account_id: str
    currency: str

    @field_validator("venue")
    @classmethod
    def _practice_venue_only(cls, value: str) -> str:
        if value not in PRACTICE_VENUES:
            raise ValueError("a ledger binding names a practice venue")
        return value

    @field_validator("account_id")
    @classmethod
    def _account_id_shape(cls, value: str) -> str:
        if _ACCOUNT_ID_RE.fullmatch(value) is None:
            raise ValueError("account id is malformed")
        return value

    @field_validator("currency")
    @classmethod
    def _gbp_only(cls, value: str) -> str:
        if value != PRACTICE_CURRENCY:
            raise ValueError("practice currency must be GBP")
        return value

    def __repr__(self) -> str:
        return f"VenueBinding(venue={self.venue!r})"


def allowed_mode(binding: Optional[VenueBinding]) -> OrderMode:
    """The one mode a ledger accepts: its venue's, or PAPER when it has no binding."""

    return OrderMode.PAPER if binding is None else VENUE_MODE[binding.venue]


def intent_refusal(
    mode: object,
    broker: object,
    account: object,
    binding: Optional[VenueBinding],
) -> Optional[str]:
    """Return a refusal code when a ledger may not accept this order, else None.

    LIVE is refused first and in every ledger. A paper ledger (no binding)
    accepts PAPER only, with the same checks it had before this seam. A
    practice ledger accepts PRACTICE only, from its own venue, on its own
    bound account.
    """

    text = str(getattr(mode, "value", mode)).upper()
    if text == OrderMode.LIVE.value:
        return LIVE_DISABLED
    if text != allowed_mode(binding).value:
        return MODE_VENUE_MISMATCH
    if binding is None:
        return None
    if str(broker) != binding.venue:
        return BROKER_VENUE_MISMATCH
    if str(account) != binding.account_id:
        return ACCOUNT_BINDING_MISMATCH
    return None


def refusal_text(code: str) -> str:
    """A message for a refusal code. It never carries an account id."""

    return _REFUSAL_TEXT[code]


def execution_mode_label(authority: bool, binding: Optional[VenueBinding]) -> str:
    """``practice``, ``paper`` or ``disabled``: the one status vocabulary."""

    if not authority:
        return "disabled"
    return "paper" if binding is None else "practice"


class VenueDispatcher(Protocol):
    async def dispatch(self, intent: OrderIntent) -> OrderAck: ...


@dataclass(frozen=True)
class PracticeCaps:
    """Practice caps loaded from private/uk/limits.json. Values stay out of repr."""

    capital_cap: Decimal = field(repr=False)
    per_position_cap: Decimal = field(repr=False)


@dataclass(frozen=True)
class VenueContext:
    """What a factory may read when it builds a dispatcher."""

    workspace: Workspace
    venue: str
    binding: Optional[VenueBinding] = None
    caps: Optional[PracticeCaps] = None


DispatcherFactory = Callable[[VenueContext], VenueDispatcher]
DispatcherFactoryMap = Mapping[str, DispatcherFactory]


def _paper_factory(_context: VenueContext) -> VenueDispatcher:
    from .paper_dispatcher import PaperDispatcher

    return PaperDispatcher()


def production_dispatcher_factories() -> dict[str, DispatcherFactory]:
    """The production map. ``paper`` only: no code path here reaches a broker."""

    return {VENUE_PAPER: _paper_factory}


def resolve_factory(
    venue: str, factories: Optional[DispatcherFactoryMap] = None
) -> DispatcherFactory:
    """Return the factory for ``venue`` or raise. There is no fallback to paper."""

    if venue not in KNOWN_VENUES:
        raise VenueError("VENUE_UNKNOWN", str(venue)[:40])
    table = production_dispatcher_factories() if factories is None else factories
    factory = table.get(venue)
    if factory is None:
        raise VenueError("VENUE_UNAVAILABLE", venue)
    return factory


def select_dispatcher(
    context: VenueContext, factories: Optional[DispatcherFactoryMap] = None
) -> VenueDispatcher:
    """Build the dispatcher the seam chose for ``context.venue``."""

    return resolve_factory(context.venue, factories)(context)


__all__ = [
    "ACCOUNT_BINDING_MISMATCH",
    "BROKER_VENUE_MISMATCH",
    "DispatcherFactory",
    "DispatcherFactoryMap",
    "KNOWN_VENUES",
    "LIVE_DISABLED",
    "MODE_VENUE_MISMATCH",
    "PRACTICE_CURRENCY",
    "PRACTICE_VENUES",
    "PracticeCaps",
    "VENUE_MODE",
    "VENUE_PAPER",
    "VENUE_T212_PRACTICE",
    "VenueBinding",
    "VenueContext",
    "VenueDispatcher",
    "VenueError",
    "allowed_mode",
    "execution_mode_label",
    "intent_refusal",
    "production_dispatcher_factories",
    "refusal_text",
    "resolve_factory",
    "select_dispatcher",
]
