"""The Mac's own India limits on the admission path (Phase 63-04, RISK-03 Mac side).

``ExecutionService.admit`` calls this guard for every India intent, before any approval
challenge can exist. The guard builds the ``risk_india.rules`` inputs from the Mac's own
ledger (positions, active buy reservations, open sells), the Mac latch file and the
caller's quote, runs ``rules.evaluate`` and, once the simulator has produced a fill, the
P-16 slippage check. Any reason code denies the admission; nothing here ever relaxes a
decision the existing admission flow makes.

Reason names are the ``growin-orders/1`` O6 names the VM uses (``capital_cap``,
``per_position_cap``, ``collar``, ``circuit_band``, ``off_tick``, ``session_closed``,
``halt_latch``, ``pilot_ended``, ``stop_open``, ``sell_exceeds_holding``,
``quote_unavailable``, ``state_unreadable``, ``account_read_failed`` and the rest).
``SLIPPAGE_LIMIT`` and the three Mac-only names below are documented in
``gateway/ORDERS-API.md`` under "Mac admission codes".

The quote and the VM intent are keyed by the Breeze stock code, which is not always the NSE symbol, so the
guard resolves the order's ticker through an ``IndiaInstrumentMap`` (stock code and ISIN) and checks the quote
against that. No map, or no entry, is ``instrument_unmapped``; the symbol is never a stand-in.

What the Mac cannot know and therefore does not decide: the VM kill switch (the VM refuses
on ``kill_switch``), ``mac_halt`` and ``account_mismatch`` (VM-derived; wired in 63-05).
Those inputs are passed as "clear" here, which is never less strict than the VM, because
the VM re-checks every one of them on every order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Optional

from risk_india import rules
from risk_india.state import (
    LatchStore,
    StateUnreadable,
    StateUnwritable,
    limits_from_config,
    state_path_for,
)

from .ledger import ExecutionLedger
from .models import OrderIntent, OrderSide, OrderType

# Mac-only reason codes (documented in gateway/ORDERS-API.md, "Mac admission codes").
INTENT_INVALID = "intent_invalid"
INDIA_LIMITS_UNAVAILABLE = "india_limits_unavailable"
ACCOUNT_READ_FAILED = "account_read_failed"  # an O6 name; the Mac uses it for its own ledger read
QUOTE_UNAVAILABLE = "quote_unavailable"
# Mac only: the guard holds no (or an ambiguous) binding of this ticker to its Breeze stock code and ISIN.
# It is not ``instrument_unsupported`` (the VM's word for "not a tradable cash name"): the name may be
# tradable, the Mac just cannot say which quote belongs to it, so it never guesses one from the symbol.
INSTRUMENT_UNMAPPED = "instrument_unmapped"

DEFAULT_MAX_QUOTE_AGE_SECONDS = 30
_TICKER_PREFIX = "NSE:CASH:"


class IndiaLimitDenied(ValueError):
    """An India limit refused the order. ``code`` is the reason recorded on the admission."""

    def __init__(self, code: str, codes: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.codes = codes or (code,)


@dataclass(frozen=True)
class IndiaQuoteEvidence:
    """One India quote for one admission: the rule inputs and when they were read.

    ``rules.slippage_check`` takes no freshness input (63-02 review), so the guard refuses a
    quote older than ``max_quote_age_seconds`` (or from the future) before it calls anything.
    """

    quote: rules.Quote
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.quote, rules.Quote):
            raise TypeError("quote must be a risk_india Quote")
        if self.observed_at.tzinfo is None:
            raise ValueError("quote observation time must be timezone-aware")


@dataclass(frozen=True)
class IndiaInstrumentBinding:
    """One ticker's Breeze stock code and, when the caller knows it, its ISIN.

    ``isin`` is None only for the explicit ``symbol_is_stock_code`` map (see there).
    """

    stock_code: str
    isin: Optional[str]


class IndiaInstrumentMap:
    """The caller's own record of ``NSE:CASH:<symbol>`` to (Breeze stock code, ISIN).

    The order's symbol is the NSE trading symbol and the quote and the VM intent are keyed by the Breeze
    stock code. The two differ for many names (CANBK trades as CANBAN at Breeze), so the guard must ask this
    map for the code instead of reading it off the symbol. The map is built once from the caller's own
    instrument list, never from a quote, so a quote cannot vouch for itself. Conflicting entries (one ticker,
    code or ISIN twice) refuse the whole map.
    """

    def __init__(self, bindings: Mapping[str, IndiaInstrumentBinding], *, symbol_is_code: bool = False) -> None:
        self._bindings = dict(bindings)
        self._symbol_is_code = symbol_is_code

    @classmethod
    def from_instruments(cls, instruments: Iterable[Any]) -> "IndiaInstrumentMap":
        """Build from objects with ``symbol``, ``stock_code`` and ``isin`` (``shadow_india.portfolio.Instrument``)."""

        bound: dict[str, IndiaInstrumentBinding] = {}
        codes: set[str] = set()
        isins: set[str] = set()
        for item in instruments:
            symbol, code, isin = item.symbol, item.stock_code, item.isin
            if not all(isinstance(v, str) and v for v in (symbol, code, isin)):
                raise rules.RiskConfigError("an instrument needs a symbol, a stock code and an ISIN")
            ticker = _TICKER_PREFIX + symbol
            if ticker in bound or code in codes or isin in isins:
                raise rules.RiskConfigError("instrument mapping conflict")
            bound[ticker] = IndiaInstrumentBinding(code, isin)
            codes.add(code)
            isins.add(isin)
        return cls(bound)

    @classmethod
    def symbol_is_stock_code(cls) -> "IndiaInstrumentMap":
        """Every ticker's code is its symbol. For callers whose own relay path already fetches the quote and
        builds the VM intent with the symbol as the code (the app-hosted relay mint, ``OrderRelayMinter``).
        The ISIN is then unknown here (the VM binds it from its own master, P-21), so the quote's is used."""

        return cls({}, symbol_is_code=True)

    def resolve(self, ticker: str) -> Optional[IndiaInstrumentBinding]:
        found = self._bindings.get(ticker)
        if found is not None:
            return found
        if self._symbol_is_code and ticker.startswith(_TICKER_PREFIX) and len(ticker) > len(_TICKER_PREFIX):
            return IndiaInstrumentBinding(ticker[len(_TICKER_PREFIX) :], None)
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


class IndiaAdmissionGuard:
    """Mac India limits for one open India ledger."""

    def __init__(
        self,
        *,
        limits: rules.Limits,
        max_slippage_bps: Decimal,
        store: LatchStore,
        clock: Callable[[], datetime] = _utc_now,
        max_quote_age_seconds: int = DEFAULT_MAX_QUOTE_AGE_SECONDS,
        instruments: Optional[IndiaInstrumentMap] = None,
    ) -> None:
        if instruments is not None and not isinstance(instruments, IndiaInstrumentMap):
            raise rules.RiskConfigError("instruments must be an IndiaInstrumentMap")
        if not isinstance(max_slippage_bps, Decimal) or not max_slippage_bps > 0:
            raise rules.RiskConfigError("max_slippage_bps must be a positive Decimal")
        if max_quote_age_seconds <= 0:
            raise rules.RiskConfigError("max_quote_age_seconds must be positive")
        self._limits = limits
        self._max_slippage_bps = max_slippage_bps
        self._store = store
        self._clock = clock
        self._max_quote_age_seconds = max_quote_age_seconds
        # None means no binding is known: every India order is denied ``instrument_unmapped``.
        self._instruments = instruments

    @classmethod
    def from_config(
        cls,
        config: Any,
        ledger: ExecutionLedger,
        *,
        clock: Callable[[], datetime] = _utc_now,
        instruments: Optional[IndiaInstrumentMap] = None,
    ) -> "IndiaAdmissionGuard":
        """Build the guard from a ``WorkspaceConfig`` loaded with ``require_india_execution``."""

        limits = limits_from_config(config)
        store = LatchStore(state_path_for(ledger.path), limits, has_fills=ledger.has_fills)
        return cls(
            limits=limits,
            max_slippage_bps=config.india_execution.max_slippage_bps,
            store=store,
            clock=clock,
            instruments=instruments,
        )

    @property
    def limits(self) -> rules.Limits:
        return self._limits

    @property
    def store(self) -> LatchStore:
        return self._store

    @property
    def limits_sha256(self) -> str:
        return self._limits.sha256

    # ------------------------------------------------------------------ rules

    def _usable_quote(
        self, evidence: Optional[IndiaQuoteEvidence], now: datetime
    ) -> Optional[rules.Quote]:
        if evidence is None:
            return None
        age = (now - evidence.observed_at).total_seconds()
        if age < 0 or age > self._max_quote_age_seconds:
            return None
        return evidence.quote

    def check_order(
        self,
        intent: OrderIntent,
        ledger: ExecutionLedger,
        evidence: Optional[IndiaQuoteEvidence],
        *,
        exclude_proposal_id: str = "",
        admitted_quantity: Optional[Decimal] = None,
    ) -> Mapping[str, Any]:
        """Run every Mac India rule for one intent. Raises ``IndiaLimitDenied`` on any code.

        ``exclude_proposal_id`` is for a recheck of an order that is already admitted (at
        challenge and at claim): its own reservation and open sell are left out of the caps,
        so the order does not count against itself. ``admitted_quantity`` is the quantity the
        admission settled on (the risk gate may have scaled it below the request); the intent
        stays immutable, and a recheck sizes the order at what was actually reserved.

        Returns the detail recorded with an admitted order. A BUY reads the latch file; a
        SELL never does, because halted, ended and stop latches only ever block buys, and an
        unreadable file must not trap a position.
        """

        now = self._clock()
        if now.tzinfo is None:
            raise rules.RiskConfigError("the admission clock must be timezone-aware")
        buy = intent.side is OrderSide.BUY
        quantity = intent.quantity if admitted_quantity is None else admitted_quantity
        if (
            intent.order_type is not OrderType.LIMIT
            or intent.limit_price is None
            or quantity != quantity.to_integral_value()
        ):
            raise IndiaLimitDenied(INTENT_INVALID)
        if not intent.ticker.startswith(_TICKER_PREFIX) or len(intent.ticker) == len(_TICKER_PREFIX):
            raise IndiaLimitDenied("instrument_unsupported")
        # The quote and the VM intent are keyed by the Breeze stock code, which is not always the NSE symbol.
        # No binding (or no map) is a refusal; the symbol is never used as a stand-in.
        binding = self._instruments.resolve(intent.ticker) if self._instruments is not None else None
        if binding is None:
            raise IndiaLimitDenied(INSTRUMENT_UNMAPPED)

        latch = None
        if buy:
            try:
                latch = self._store.load()
            except (StateUnreadable, StateUnwritable) as exc:
                raise IndiaLimitDenied(exc.code) from None

        try:
            view = ledger.india_account_view(exclude_proposal_id=exclude_proposal_id)
            quote = self._usable_quote(evidence, now)
            # The Mac ledger is keyed by execution ticker. The order's own position takes the
            # quote's ISIN so ``rules`` can match it; every other position keeps its ticker as
            # an opaque key (only the cap sums read those). The order's ISIN is the mapped instrument's, so
            # ``rules`` refuses a quote of another ISIN. Only the symbol-is-code map has no ISIN and takes
            # the quote's (the VM binds it from its master).
            if binding.isin is not None:
                own_key = binding.isin
            else:
                own_key = quote.isin if quote is not None else ""
            holdings = tuple(
                rules.Holding(own_key if ticker == intent.ticker else ticker, _whole(quantity), cost)
                for ticker, quantity, cost in view.positions
            )
            open_orders = tuple(
                rules.OpenOrder(
                    own_key if ticker == intent.ticker else ticker, "buy", _whole(quantity), limit
                )
                for ticker, quantity, limit in view.open_buys
            ) + tuple(
                # ``rules`` reads only the quantity of an open sell.
                rules.OpenOrder(
                    own_key if ticker == intent.ticker else ticker, "sell", _whole(quantity), Decimal(1)
                )
                for ticker, quantity in view.open_sells.items()
            )
            order = rules.OrderRequest(
                "buy" if buy else "sell",
                binding.stock_code,
                own_key,
                int(quantity),
                intent.limit_price,
            )
        except IndiaLimitDenied:
            raise
        except rules.RiskConfigError:
            raise IndiaLimitDenied(INTENT_INVALID) from None
        except (ArithmeticError, ValueError):
            raise IndiaLimitDenied(ACCOUNT_READ_FAILED) from None

        flags = (
            rules.RiskFlags(
                halt=latch.halt,
                ended=latch.ended,
                mac_halt=False,
                account_mismatch=False,
                stops=frozenset(latch.stops),
            )
            if latch is not None
            else rules.RiskFlags()
        )
        decision = rules.evaluate(
            self._limits,
            flags,
            rules.Account(holdings, open_orders),
            quote,
            now.astimezone(rules.IST),
            order,
            kill_enabled=True,
        )
        if not decision.allowed:
            raise IndiaLimitDenied(decision.first or INTENT_INVALID, decision.codes)
        return {
            "limits_sha256": self._limits.sha256,
            "latches": list(latch.latch_names()) if latch is not None else [],
        }

    # --------------------------------------------------------------- slippage

    def check_slippage(
        self,
        intent: OrderIntent,
        evidence: Optional[IndiaQuoteEvidence],
        fill_price: Decimal,
    ) -> Decimal:
        """P-16: deny when the simulated fill is worse than the quote by more than the cap.

        The reference is the side of the admission quote the order would take; the fill is the
        simulator's. Returns the side-adjusted slippage in basis points.
        """

        now = self._clock()
        quote = self._usable_quote(evidence, now)
        if quote is None:
            raise IndiaLimitDenied(QUOTE_UNAVAILABLE)
        side = "buy" if intent.side is OrderSide.BUY else "sell"
        try:
            result = rules.slippage_check(side, quote, fill_price, self._max_slippage_bps)
        except rules.RiskConfigError:
            raise IndiaLimitDenied(rules.SLIPPAGE_LIMIT) from None
        if not result.ok:
            raise IndiaLimitDenied(result.code or rules.SLIPPAGE_LIMIT)
        assert result.slippage_bps is not None
        return result.slippage_bps


def _whole(quantity: Decimal) -> int:
    """A ledger quantity as a whole share count, or ``ValueError`` (India trades whole shares)."""

    if quantity != quantity.to_integral_value() or quantity <= 0:
        raise ValueError("fractional or non-positive share quantity")
    return int(quantity)


__all__ = [
    "ACCOUNT_READ_FAILED",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "INDIA_LIMITS_UNAVAILABLE",
    "INSTRUMENT_UNMAPPED",
    "INTENT_INVALID",
    "IndiaAdmissionGuard",
    "IndiaInstrumentBinding",
    "IndiaInstrumentMap",
    "IndiaLimitDenied",
    "IndiaQuoteEvidence",
    "QUOTE_UNAVAILABLE",
]
