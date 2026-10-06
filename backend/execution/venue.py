"""The execution venue seam (Phase 66, E1).

A venue names where an order goes. ``paper`` acknowledges locally. A practice
venue is bound to one broker practice account and accepts only ``PRACTICE``
orders. This module holds the venue ids, the immutable ledger binding, the one
mode rule every gate shares, and the dispatcher factory map that
``AppState.start_execution`` selects from.

It imports only ``models`` and the stdlib-only ``venue_registry`` so the
ledger, approval and service layers can all use it without a cycle. Which venue
kinds exist, and what each one is bound to, is declared once in
``venue_registry.VENUE_SPECS``; nothing here names a venue kind in guard logic.
It contacts no broker, and the production map holds only ``paper``: a practice
dispatcher is registered by tests, or by a later plan, never here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

import venue_registry
from venue_registry import (
    VENUE_PAPER,
    VENUE_T212_PRACTICE,
    VenueSpec,
    known_venues,
    registered_kinds,
    spec_for,
)

from .models import OrderAck, OrderIntent, OrderMode, Workspace

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
    """What a bound ledger is tied to at creation. Never changes afterwards.

    ``venue`` must be a registered kind and ``currency`` must be that kind's
    currency; both come from its ``VenueSpec``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    venue: str
    account_id: str
    currency: str

    @field_validator("venue")
    @classmethod
    def _registered_venue_only(cls, value: str) -> str:
        if spec_for(value) is None:
            raise ValueError("a ledger binding names a registered venue")
        return value

    @field_validator("account_id")
    @classmethod
    def _account_id_shape(cls, value: str) -> str:
        if _ACCOUNT_ID_RE.fullmatch(value) is None:
            raise ValueError("account id is malformed")
        return value

    @model_validator(mode="after")
    def _currency_is_the_specs(self) -> "VenueBinding":
        spec = spec_for(self.venue)
        if spec is None or self.currency != spec.currency:
            raise ValueError("binding currency must be the venue's currency")
        return self

    def __repr__(self) -> str:
        return f"VenueBinding(venue={self.venue!r})"


def allowed_modes(binding: Optional[VenueBinding]) -> frozenset[str]:
    """The order modes a ledger accepts: its venue spec's, or PAPER with no binding.

    A binding whose venue is no longer registered accepts nothing.
    """

    if binding is None:
        return frozenset({OrderMode.PAPER.value})
    spec = spec_for(binding.venue)
    return frozenset() if spec is None else spec.modes


def allowed_mode(binding: Optional[VenueBinding]) -> OrderMode:
    """The one mode a single-mode ledger accepts. Raises when its spec has several."""

    modes = allowed_modes(binding)
    if len(modes) != 1:
        raise VenueError("VENUE_MODE_NOT_SINGLE", "" if binding is None else binding.venue)
    return OrderMode(next(iter(modes)))


def intent_refusal(
    mode: object,
    broker: object,
    account: object,
    binding: Optional[VenueBinding],
) -> Optional[str]:
    """Return a refusal code when a ledger may not accept this order, else None.

    LIVE is refused first and in every ledger. A paper ledger (no binding)
    accepts PAPER only, with the same checks it had before this seam. A bound
    ledger accepts only the modes its venue spec lists, from its own venue, on
    its own bound account. The rule reads the spec; it names no venue kind.
    """

    text = str(getattr(mode, "value", mode)).upper()
    if text == venue_registry.LIVE_MODE:
        return LIVE_DISABLED
    if text not in allowed_modes(binding):
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


# Where an admission price may come from in a bound-venue ledger (Phase 66, D-02).
# ``operator-recorded`` is a recorded-quote replay the operator typed; the other is
# the test fixture replay. A Yahoo price or a ``Position.currentPrice`` is in neither.
PRICE_SOURCE_OPERATOR_RECORDED = "operator-recorded"
PRICE_SOURCE_TEST_REPLAY = "local-replay"
ADMISSIBLE_PRICE_SOURCES = frozenset(
    {PRICE_SOURCE_OPERATOR_RECORDED, PRICE_SOURCE_TEST_REPLAY}
)


@dataclass(frozen=True)
class CancelResult:
    """What one cancel attempt did. ``REQUESTED`` means accepted, not cancelled (D-22)."""

    outcome: str  # REQUESTED, REFUSED or UNKNOWN
    code: str

    @property
    def requested(self) -> bool:
        return self.outcome == "REQUESTED"


class VenueDispatcher(Protocol):
    async def dispatch(self, intent: OrderIntent) -> OrderAck: ...


class VenueCanceller(Protocol):
    """Optional capability: a dispatcher that can request a cancel (63 reuses this shape)."""

    async def cancel(self, broker_order_id: str) -> CancelResult: ...


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


def _t212_practice_factory(context: VenueContext) -> VenueDispatcher:
    """The practice adapter. It reaches the demo host only and needs the practice key pair."""

    from brokers.trading212.practice_dispatcher import practice_factory

    return practice_factory()(context)


def production_dispatcher_factories() -> dict[str, DispatcherFactory]:
    """The production map: ``paper`` and the practice (demo-only) adapter.

    Nothing here can reach a live broker host: the practice adapter defines only
    the demo base URL and refuses any other host before a request is sent.
    """

    return {VENUE_PAPER: _paper_factory, VENUE_T212_PRACTICE: _t212_practice_factory}


def resolve_factory(
    venue: str, factories: Optional[DispatcherFactoryMap] = None
) -> DispatcherFactory:
    """Return the factory for ``venue`` or raise. There is no fallback to paper."""

    if venue == VENUE_PAPER:
        key = VENUE_PAPER
    else:
        spec = spec_for(venue)
        if spec is None:
            raise VenueError("VENUE_UNKNOWN", str(venue)[:40])
        key = spec.dispatcher_key
    table = production_dispatcher_factories() if factories is None else factories
    factory = table.get(key)
    if factory is None:
        raise VenueError("VENUE_UNAVAILABLE", venue)
    return factory


def select_dispatcher(
    context: VenueContext, factories: Optional[DispatcherFactoryMap] = None
) -> VenueDispatcher:
    """Build the dispatcher the seam chose for ``context.venue``."""

    return resolve_factory(context.venue, factories)(context)


__all__ = [
    "ADMISSIBLE_PRICE_SOURCES",
    "CancelResult",
    "PRICE_SOURCE_OPERATOR_RECORDED",
    "PRICE_SOURCE_TEST_REPLAY",
    "VenueCanceller",
    "ACCOUNT_BINDING_MISMATCH",
    "BROKER_VENUE_MISMATCH",
    "DispatcherFactory",
    "DispatcherFactoryMap",
    "LIVE_DISABLED",
    "MODE_VENUE_MISMATCH",
    "PracticeCaps",
    "VENUE_PAPER",
    "VENUE_T212_PRACTICE",
    "VenueBinding",
    "VenueContext",
    "VenueDispatcher",
    "VenueError",
    "VenueSpec",
    "allowed_mode",
    "allowed_modes",
    "execution_mode_label",
    "intent_refusal",
    "known_venues",
    "production_dispatcher_factories",
    "refusal_text",
    "registered_kinds",
    "resolve_factory",
    "select_dispatcher",
    "spec_for",
]
