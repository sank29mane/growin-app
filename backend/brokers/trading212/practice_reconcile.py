"""On-demand reconciliation of practice orders against Trading 212 (66-03, D-15, D-17).

One call to ``PracticeReconciler.reconcile`` reads, in this order and always
through the governor:

1. the pending order by id (a 404 means "not pending", never FAILED), or, for an
   UNKNOWN order that has no id yet, the pending list;
2. ``history/orders?ticker=``, following ``nextPagePath`` until it is null,
   which supplies the terminal state and the fill prices;
3. ``positions`` as a cross-check of the ledger's own position.

It then applies one monotonic snapshot through the ledger. There is no
background task. The bounded poll (default 120 s, injected clock and sleep)
exists only for the gap between a fill and its appearance in history, and for
an UNKNOWN order whose broker copy is not visible yet.

An UNKNOWN order is matched strictly: ``initiatedFrom == API``, type LIMIT,
ticker, signed quantity, ``limitPrice`` and a ``createdAt`` inside
[send - 5 s, send + 120 s]. One match adopts its id, none leaves the order
UNKNOWN, and two or more escalate with nothing adopted. A broker id that the
ledger already holds is never adopted again. Broker JSON is untrusted: every
field is type-checked, an unknown status maps to UNKNOWN, and the ledger's own
monotonic and notional checks stay in force.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping, Optional

from execution.ledger import ExecutionLedger, LedgerError
from execution.models import (
    OrderSide,
    ReconciliationSnapshot,
    ReconciliationStatus,
)
from execution.venue import VenueBinding

from .practice_transport import PracticeTransport, PracticeTransportError

SOURCE = "t212-practice-reconcile"
DEFAULT_WINDOW_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 5.0
MATCH_BEFORE_SECONDS = 5.0
MATCH_AFTER_SECONDS = 120.0
HISTORY_PAGE_LIMIT = 50
MAX_HISTORY_PAGES = 30
# A fill's price-based value may differ from the account-currency value by tax
# and fees, never by a factor of 100 (a GBX versus GBP mix-up).
FILL_VALUE_TOLERANCE = Decimal("0.10")

_OPEN_STATUSES = frozenset({"LOCAL", "UNCONFIRMED", "CONFIRMED", "NEW", "CANCELLING"})
_RECONCILABLE = frozenset({"ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"})


class ReconcileRefused(Exception):
    """The reconcile cannot run. Carries a stable code and no broker text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ReconcileResult:
    proposal_id: str
    state_before: str
    state: str
    code: str
    broker_order_id: Optional[str] = None
    cumulative_quantity: Optional[str] = None
    cumulative_notional: Optional[str] = None
    position_check: str = "SKIPPED"
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "state_before": self.state_before,
            "state": self.state,
            "code": self.code,
            "broker_order_id": self.broker_order_id,
            "cumulative_quantity": self.cumulative_quantity,
            "cumulative_notional": self.cumulative_notional,
            "position_check": self.position_check,
        }


def _decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    # 2.0 and 2 are one quantity; 2E+1 is 20. Keep the stored text plain.
    parsed = parsed.normalize()
    return parsed.quantize(Decimal(1)) if parsed.as_tuple().exponent > 0 else parsed


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _int_id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def signed_quantity(order: Mapping[str, Any]) -> Optional[Decimal]:
    """The order's signed quantity, or None when it contradicts itself.

    With a ``side`` the magnitude is taken from ``quantity`` and the sign from
    the side (and a quantity whose own sign disagrees is refused). Without one,
    the sign of ``quantity`` is the side, as in the request convention.
    """

    quantity = _decimal(order.get("quantity"))
    if quantity is None or quantity == 0:
        return None
    side = order.get("side")
    if side == "BUY":
        return quantity if quantity > 0 else None
    if side == "SELL":
        return -abs(quantity)
    if side is None:
        return quantity
    return None


class PracticeReconciler:
    def __init__(
        self,
        transport: PracticeTransport,
        ledger: ExecutionLedger,
        binding: VenueBinding,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._transport = transport
        self._ledger = ledger
        self._binding = binding
        self._clock = clock
        self._sleep = sleep
        self._window = min(float(window_seconds), DEFAULT_WINDOW_SECONDS)
        self._poll = max(float(poll_seconds), 1.0)

    # --- broker reads (every one governed by the transport) -------------------

    async def _get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        try:
            response = await self._transport.get(path, params=params)
        except PracticeTransportError as exc:
            raise ReconcileRefused(f"READ_{exc.kind.upper()}") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ReconcileRefused(f"READ_HTTP_{response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ReconcileRefused("READ_NOT_JSON") from None

    async def _pending_by_id(self, order_id: str) -> Optional[Mapping[str, Any]]:
        body = await self._get_json(f"/equity/orders/{int(order_id)}")
        if body is None:
            return None
        if not isinstance(body, Mapping):
            raise ReconcileRefused("READ_INVALID")
        return body

    async def _pending_list(self) -> list[Mapping[str, Any]]:
        body = await self._get_json("/equity/orders")
        if body is None:
            return []
        if not isinstance(body, list):
            raise ReconcileRefused("READ_INVALID")
        return [item for item in body if isinstance(item, Mapping)]

    async def _history(self, ticker: str) -> list[Mapping[str, Any]]:
        """Every ``{order, fill}`` item for the ticker, following ``nextPagePath`` to null."""

        items: list[Mapping[str, Any]] = []
        path: Optional[str] = "/equity/history/orders"
        params: Optional[Mapping[str, Any]] = {"ticker": ticker, "limit": HISTORY_PAGE_LIMIT}
        for _ in range(MAX_HISTORY_PAGES):
            body = await self._get_json(path, params=params)
            if body is None:
                return items
            if not isinstance(body, Mapping) or not isinstance(body.get("items"), list):
                raise ReconcileRefused("READ_INVALID")
            items.extend(item for item in body["items"] if isinstance(item, Mapping))
            next_path = body.get("nextPagePath")
            if not next_path:
                return items
            if not isinstance(next_path, str):
                raise ReconcileRefused("READ_INVALID")
            path, params = next_path, None
        raise ReconcileRefused("HISTORY_TOO_LONG")

    # --- entry point -----------------------------------------------------------

    async def reconcile(self, proposal_id: str) -> ReconcileResult:
        order = self._ledger.get_order(proposal_id)
        if order is None:
            raise ReconcileRefused("ORDER_NOT_FOUND")
        before = order.state
        if before not in _RECONCILABLE:
            return ReconcileResult(proposal_id, before, before, "NOT_RECONCILABLE")
        if str(order.intent.get("account")) != self._binding.account_id:
            raise ReconcileRefused("ACCOUNT_BINDING_MISMATCH")
        admission = self._ledger.get_admission(proposal_id)
        if admission is None:
            raise ReconcileRefused("ADMISSION_MISSING")

        deadline = self._clock() + self._window
        result: Optional[ReconcileResult] = None
        while True:
            if order.acknowledgment is not None:
                result = await self._reconcile_known(order, before)
            else:
                result = await self._reconcile_unknown(order, before)
            if result.code not in {"NOT_VISIBLE", "FILL_EVIDENCE_PENDING", "NO_MATCH"}:
                break
            if self._clock() >= deadline:
                break
            await self._sleep(self._poll)
            refreshed = self._ledger.get_order(proposal_id)
            if refreshed is not None:
                order = refreshed
        if result.code in {"APPLIED", "UNCHANGED"} or result.state in {"FILLED", "PARTIALLY_FILLED"}:
            result = await self._with_position_check(result, order)
        return result

    # --- known broker id ---------------------------------------------------------

    async def _reconcile_known(self, order: Any, before: str) -> ReconcileResult:
        broker_id = order.acknowledgment.broker_order_id
        ticker = str(order.intent["ticker"])
        pending = await self._pending_by_id(broker_id)
        history = await self._history(ticker)
        return self._apply(order, before, broker_id, pending, history)

    def _apply(
        self,
        order: Any,
        before: str,
        broker_id: str,
        pending: Optional[Mapping[str, Any]],
        history: list[Mapping[str, Any]],
        *,
        adopted: bool = False,
    ) -> ReconcileResult:
        proposal_id = order.proposal_id
        mine = [
            item
            for item in history
            if isinstance(item.get("order"), Mapping) and str(item["order"].get("id")) == broker_id
        ]
        fills = [item["fill"] for item in mine if isinstance(item.get("fill"), Mapping)]
        order_obj: Optional[Mapping[str, Any]] = pending
        if order_obj is None and mine:
            order_obj = self._latest(mine)
        if order_obj is None:
            # 404 by id and absent from history: not pending is not failed. Wait for
            # history (fill-to-history delay) and otherwise leave the order as it is.
            return ReconcileResult(proposal_id, before, before, "NOT_VISIBLE", broker_id)

        status = order_obj.get("status")
        ordered = _decimal(order_obj.get("quantity"))
        cumulative_qty = Decimal("0")
        cumulative_notional = Decimal("0")
        for fill in fills:
            qty = _decimal(fill.get("quantity"))
            price = _decimal(fill.get("price"))
            if qty is None or price is None or qty <= 0 or price <= 0:
                return self._anomaly(order, before, broker_id, "FILL_INVALID")
            scale = self._price_scale(order_obj)
            if scale is None:
                return self._anomaly(order, before, broker_id, "CURRENCY_NOT_SUPPORTED")
            value = qty * price / scale
            wallet = fill.get("walletImpact")
            net = _decimal(wallet.get("netValue")) if isinstance(wallet, Mapping) else None
            if net is not None and net != 0:
                if abs(value - abs(net)) > abs(net) * FILL_VALUE_TOLERANCE:
                    return self._anomaly(order, before, broker_id, "FILL_VALUE_MISMATCH")
            cumulative_qty += qty
            cumulative_notional += value
        reported_filled = _decimal(order_obj.get("filledQuantity")) or Decimal("0")
        if status == "FILLED":
            if ordered is None or ordered == 0:
                return self._anomaly(order, before, broker_id, "ORDER_QUANTITY_INVALID")
            if cumulative_qty > abs(ordered):
                return self._anomaly(order, before, broker_id, "FILL_EXCEEDS_ORDER")
            if cumulative_qty < abs(ordered):
                # Filled, but its fills are not in history yet (A7): wait, never guess.
                return ReconcileResult(
                    proposal_id, before, before, "FILL_EVIDENCE_PENDING", broker_id
                )
        elif status == "PARTIALLY_FILLED" and reported_filled > cumulative_qty:
            return ReconcileResult(proposal_id, before, before, "FILL_EVIDENCE_PENDING", broker_id)

        target = self._map_status(status, cumulative_qty, ordered)
        if target is None:
            target = ReconciliationStatus.UNKNOWN
        snapshot = ReconciliationSnapshot(
            proposal_id=proposal_id,
            broker_order_id=broker_id,
            source=SOURCE,
            cumulative_quantity=cumulative_qty,
            cumulative_notional=cumulative_notional,
            status=target,
            evidence_fingerprint=self._fingerprint(broker_id, target, fills),
            observed_at=datetime.fromtimestamp(self._clock(), tz=timezone.utc),
        )
        try:
            updated = self._ledger.reconcile(snapshot)
        except LedgerError as exc:
            return self._anomaly(order, before, broker_id, self._ledger_code(exc))
        code = "ADOPTED" if adopted else ("APPLIED" if updated.state != before else "UNCHANGED")
        return ReconcileResult(
            proposal_id,
            before,
            updated.state,
            code,
            broker_id,
            str(cumulative_qty),
            str(cumulative_notional),
        )

    @staticmethod
    def _latest(items: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        best = items[0]["order"]
        best_at = -1.0
        for item in items:
            filled = _epoch((item.get("fill") or {}).get("filledAt"))
            if filled is not None and filled >= best_at:
                best, best_at = item["order"], filled
        return best

    @staticmethod
    def _price_scale(order: Mapping[str, Any]) -> Optional[Decimal]:
        """100 for pence-quoted (GBX) instruments, 1 for GBP, None for anything else."""

        instrument = order.get("instrument")
        currency = instrument.get("currency") if isinstance(instrument, Mapping) else None
        if currency is None:
            currency = order.get("currency")
        if currency == "GBX":
            return Decimal("100")
        if currency == "GBP":
            return Decimal("1")
        return None

    @staticmethod
    def _map_status(
        status: Any, cumulative_qty: Decimal, ordered: Optional[Decimal]
    ) -> Optional[ReconciliationStatus]:
        if status in _OPEN_STATUSES:
            return (
                ReconciliationStatus.PARTIALLY_FILLED
                if cumulative_qty > 0
                else ReconciliationStatus.ACKNOWLEDGED
            )
        if status == "PARTIALLY_FILLED":
            return (
                ReconciliationStatus.PARTIALLY_FILLED
                if cumulative_qty > 0
                else ReconciliationStatus.ACKNOWLEDGED
            )
        if status == "FILLED":
            # Reached only when the fills already add up to the whole order.
            return ReconciliationStatus.FILLED
        if status == "CANCELLED":
            return ReconciliationStatus.CANCELLED
        if status == "REJECTED":
            return ReconciliationStatus.REJECTED
        # REPLACING, REPLACED and anything unrecognised: never a guess.
        return ReconciliationStatus.UNKNOWN

    @staticmethod
    def _fingerprint(broker_id: str, target: ReconciliationStatus, fills: list[Any]) -> str:
        parts = sorted(
            f"{fill.get('id')}:{fill.get('quantity')}:{fill.get('price')}"
            for fill in fills
            if isinstance(fill, Mapping)
        )
        raw = json.dumps([broker_id, target.value, parts], separators=(",", ":"))
        return "t212:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]

    @staticmethod
    def _ledger_code(exc: Exception) -> str:
        text = str(exc)
        if "non-monotonic" in text:
            return "NON_MONOTONIC"
        if "overfills" in text:
            return "OVERFILL"
        if "admitted notional" in text:
            return "NOTIONAL_EXCEEDED"
        if "cannot reconcile" in text:
            return "ILLEGAL_TRANSITION"
        return "LEDGER_REFUSED"

    def _anomaly(self, order: Any, before: str, broker_id: Optional[str], code: str) -> ReconcileResult:
        try:
            self._ledger.record_audit_event(
                order.proposal_id, "RECONCILIATION_ANOMALY", {"code": code}
            )
        except LedgerError:
            pass
        current = self._ledger.get_order(order.proposal_id)
        return ReconcileResult(
            order.proposal_id,
            before,
            before if current is None else current.state,
            code,
            broker_id,
        )

    # --- UNKNOWN with no broker id --------------------------------------------------

    async def _reconcile_unknown(self, order: Any, before: str) -> ReconcileResult:
        ticker = str(order.intent["ticker"])
        pending = await self._pending_list()
        history = await self._history(ticker)
        matches = self._match(order, pending, history)
        if not matches:
            return ReconcileResult(order.proposal_id, before, before, "NO_MATCH")
        if len(matches) > 1:
            try:
                self._ledger.record_audit_event(
                    order.proposal_id,
                    "RECONCILIATION_ESCALATED",
                    {"code": "AMBIGUOUS_MATCH", "matches": len(matches)},
                )
            except LedgerError:
                pass
            return ReconcileResult(
                order.proposal_id, before, before, "AMBIGUOUS_MATCH", detail={"matches": len(matches)}
            )
        broker_id = matches[0]
        pending_obj = next((o for o in pending if str(o.get("id")) == broker_id), None)
        return self._apply(order, before, broker_id, pending_obj, history, adopted=True)

    def _match(
        self, order: Any, pending: list[Mapping[str, Any]], history: list[Mapping[str, Any]]
    ) -> list[str]:
        intent = order.intent
        attempts = self._ledger.list_attempts(order.proposal_id)
        if not attempts:
            return []
        sent_at = _epoch(attempts[-1].claimed_at)
        if sent_at is None:
            return []
        wanted_qty = Decimal(str(intent["quantity"]))
        if str(intent.get("side")) == OrderSide.SELL.value:
            wanted_qty = -wanted_qty
        wanted_price = Decimal(str(intent["limit_price"]))
        known = self._ledger.known_broker_order_ids()
        candidates: dict[str, Mapping[str, Any]] = {}
        for candidate in [*pending, *(item["order"] for item in history if isinstance(item.get("order"), Mapping))]:
            candidates.setdefault(str(candidate.get("id")), candidate)
        found: list[str] = []
        for key, candidate in candidates.items():
            if _int_id(candidate.get("id")) is None or key in known:
                continue
            created = _epoch(candidate.get("createdAt"))
            price = _decimal(candidate.get("limitPrice"))
            if (
                candidate.get("initiatedFrom") == "API"
                and candidate.get("type") == "LIMIT"
                and candidate.get("ticker") == intent.get("ticker")
                and signed_quantity(candidate) == wanted_qty
                and price is not None
                and price == wanted_price
                and created is not None
                and sent_at - MATCH_BEFORE_SECONDS <= created <= sent_at + MATCH_AFTER_SECONDS
            ):
                found.append(key)
        return sorted(found)

    # --- cross-check ----------------------------------------------------------------

    async def _with_position_check(self, result: ReconcileResult, order: Any) -> ReconcileResult:
        ticker = str(order.intent["ticker"])
        try:
            body = await self._get_json("/equity/positions", params={"ticker": ticker})
        except ReconcileRefused:
            return self._with(result, "POSITION_CHECK_FAILED")
        if not isinstance(body, list):
            return self._with(result, "POSITION_CHECK_FAILED")
        broker_quantity = Decimal("0")
        for item in body:
            instrument = item.get("instrument") if isinstance(item, Mapping) else None
            if isinstance(instrument, Mapping) and instrument.get("ticker") == ticker:
                broker_quantity = _decimal(item.get("quantity")) or Decimal("0")
        held = self._ledger.get_paper_position(
            self._binding.account_id,
            self._binding.currency,
            ticker,
            workspace=self._ledger.workspace,
        )
        ledger_quantity = Decimal(held["quantity"]) if held else Decimal("0")
        if broker_quantity < ledger_quantity:
            try:
                self._ledger.record_audit_event(
                    order.proposal_id, "RECONCILIATION_ANOMALY", {"code": "POSITION_MISMATCH"}
                )
            except LedgerError:
                pass
            return self._with(result, "POSITION_MISMATCH")
        return self._with(result, "OK")

    @staticmethod
    def _with(result: ReconcileResult, check: str) -> ReconcileResult:
        return ReconcileResult(
            result.proposal_id,
            result.state_before,
            result.state,
            result.code,
            result.broker_order_id,
            result.cumulative_quantity,
            result.cumulative_notional,
            check,
            result.detail,
        )
