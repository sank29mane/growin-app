"""Direct Trading 212 practice dispatcher behind the execution seam (66-03).

``T212PracticeDispatcher`` implements the seam's ``dispatch`` and the optional
``cancel`` for venue ``t212_practice``. It sends one LIMIT DAY order per
approved intent, to the demo host only (``practice_transport``), and classifies
the one answer it gets (D-15):

* FAILED, because nothing reached the broker or it refused: ConnectError,
  ConnectTimeout, 400, 401, 403, and any guard that refuses before a request.
* ACKNOWLEDGED: 200 with an integer ``id``.
* UNKNOWN: 408, 429, any 5xx, a read or write timeout, a dropped connection,
  a 200 without an id, and any status the table does not name. UNKNOWN is
  never turned into FAILED here; the reconciler resolves it from broker data.

No order is ever resent. This module reads no environment variable; the key
pair is read once, by the factory, through ``workspace_credentials.uk_credential``
(D-07). It imports neither the MCP server nor ``mcp_client`` (D-12). Time comes
only from the injected clock.
"""

from __future__ import annotations

import asyncio
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

import workspace_credentials
from execution.models import OrderAck, OrderIntent, OrderMode, OrderType, Workspace
from execution.service import BrokerExecutionError, BrokerOutcomeUnknownError
from execution.venue import (
    VENUE_T212_PRACTICE,
    CancelResult,
    VenueBinding,
    VenueContext,
    VenueError,
)

from .practice_metadata import PracticeMetadata
from .practice_transport import PracticeTransport, PracticeTransportError

PRACTICE_KEY_NAME = "TRADING212_PRACTICE_API_KEY"
PRACTICE_SECRET_NAME = "TRADING212_PRACTICE_API_SECRET"

_TICKER_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_ACK_RAW_KEYS = ("id", "status", "ticker", "type", "quantity", "limitPrice", "createdAt")
_FAILED_STATUSES = frozenset({400, 401, 403})
_UNKNOWN_STATUSES = frozenset({408, 429})


class PracticeOrderRefused(BrokerExecutionError):
    """A local guard refused the order before any request. Carries a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__("Practice order refused before dispatch")
        self.code = code
        self.reason_code = code


class PracticePinError(Exception):
    """The practice account could not be proven to be the bound one."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_limit_body(
    *,
    ticker: str,
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    order_type: str = "LIMIT",
    time_validity: str = "DAY",
    extended_hours: bool = False,
) -> dict[str, Any]:
    """The one request body this adapter can build (D-04), or a refusal.

    Market, stop, stop-limit, any ``timeValidity`` other than DAY, extended
    hours, a fractional or non-positive quantity and a non-positive price are
    refused here, before any request exists. A SELL carries a negative
    ``quantity``; the ledger keeps the positive quantity and the side.
    """

    if order_type != "LIMIT":
        raise PracticeOrderRefused("ORDER_TYPE_REFUSED")
    if time_validity != "DAY":
        raise PracticeOrderRefused("TIME_VALIDITY_REFUSED")
    if extended_hours:
        raise PracticeOrderRefused("EXTENDED_HOURS_REFUSED")
    if side not in ("BUY", "SELL"):
        raise PracticeOrderRefused("SIDE_REFUSED")
    if not isinstance(ticker, str) or _TICKER_RE.fullmatch(ticker) is None:
        raise PracticeOrderRefused("TICKER_REFUSED")
    try:
        quantity = Decimal(quantity)
        limit_price = Decimal(limit_price)
    except (InvalidOperation, TypeError, ValueError):
        raise PracticeOrderRefused("QUANTITY_NOT_WHOLE") from None
    if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
        raise PracticeOrderRefused("QUANTITY_NOT_WHOLE")
    if not limit_price.is_finite() or limit_price <= 0:
        raise PracticeOrderRefused("LIMIT_PRICE_REFUSED")
    whole = int(quantity)
    return {
        "ticker": ticker,
        "quantity": -whole if side == "SELL" else whole,
        "limitPrice": float(limit_price),
        "timeValidity": "DAY",
    }


def _int_id(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _safe_raw(body: Mapping[str, Any]) -> dict[str, Any]:
    """A short allow-list of scalar order fields. Never a header, key or free text."""

    raw: dict[str, Any] = {}
    for name in _ACK_RAW_KEYS:
        value = body.get(name)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            raw[name] = value
    return raw


class T212PracticeDispatcher:
    """One practice account: one transport, one pin, one send per order."""

    broker = VENUE_T212_PRACTICE

    def __init__(
        self,
        transport: PracticeTransport,
        binding: VenueBinding,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.binding = binding
        self._clock = clock
        self._sleep = sleep
        self.metadata = PracticeMetadata(transport, clock=clock)
        self.reconciler: Any = None
        self._pinned = False

    def __repr__(self) -> str:
        return "T212PracticeDispatcher(venue='t212_practice')"

    # --- seam hooks ----------------------------------------------------------

    def attach(self, ledger: Any) -> None:
        """Called by ``AppState`` once the ledger is open; builds the reconciler."""

        from .practice_reconcile import PracticeReconciler

        self.reconciler = PracticeReconciler(
            self.transport, ledger, self.binding, clock=self._clock, sleep=self._sleep
        )

    @property
    def execution_ready(self) -> bool:
        """False until ``verify_account`` has matched the bound account (D-13)."""

        return self._pinned

    async def verify_account(self) -> None:
        """Pin check: ``account/summary`` must name the bound id and currency (D-13)."""

        self._pinned = False
        try:
            response = await self.transport.get("/equity/account/summary")
        except PracticeTransportError as exc:
            code = "PIN_UNREACHABLE" if exc.kind != "host_refused" else "PIN_HOST_REFUSED"
            raise PracticePinError(code) from None
        if response.status_code in (401, 403):
            raise PracticePinError("PIN_AUTH_FAILED")
        if response.status_code != 200:
            raise PracticePinError("PIN_RESPONSE_INVALID")
        try:
            body = response.json()
        except ValueError:
            raise PracticePinError("PIN_RESPONSE_INVALID") from None
        if not isinstance(body, Mapping):
            raise PracticePinError("PIN_RESPONSE_INVALID")
        account_id = body.get("id")
        if isinstance(account_id, bool) or not isinstance(account_id, (int, str)):
            raise PracticePinError("PIN_RESPONSE_INVALID")
        if str(account_id) != self.binding.account_id:
            raise PracticePinError("PIN_ACCOUNT_MISMATCH")
        if body.get("currency") != self.binding.currency:
            raise PracticePinError("PIN_CURRENCY_MISMATCH")
        self._pinned = True

    # --- dispatch ------------------------------------------------------------

    def _check_scope(self, intent: OrderIntent) -> None:
        if (
            workspace_credentials.process_workspace() != Workspace.UK.value
            or intent.workspace != Workspace.UK
            or intent.broker != VENUE_T212_PRACTICE
            or intent.mode is not OrderMode.PRACTICE
            or intent.account != self.binding.account_id
        ):
            raise PracticeOrderRefused("SCOPE_REFUSED")

    async def dispatch(self, intent: OrderIntent) -> OrderAck:
        self._check_scope(intent)
        if not self._pinned:
            raise PracticeOrderRefused("ACCOUNT_NOT_PINNED")
        if intent.order_type is not OrderType.LIMIT or intent.limit_price is None:
            raise PracticeOrderRefused("ORDER_TYPE_REFUSED")
        body = build_limit_body(
            ticker=intent.ticker,
            side=intent.side.value,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
        )
        return await self.send_limit_order(intent.proposal_id, body)

    async def send_limit_order(self, proposal_id: str, body: Mapping[str, Any]) -> OrderAck:
        """Send one already-built LIMIT DAY body once and classify the answer."""

        try:
            response = await self.transport.post_limit_order(body)
        except PracticeTransportError as exc:
            if exc.sent:
                raise _unknown(f"TRANSPORT_{exc.kind.upper()}") from None
            raise _failed(f"NOT_SENT_{exc.kind.upper()}") from None
        status = response.status_code
        if status == 200:
            try:
                payload = response.json()
            except ValueError:
                raise _unknown("RESPONSE_NOT_JSON") from None
            order_id = _int_id(payload.get("id")) if isinstance(payload, Mapping) else None
            if order_id is None:
                raise _unknown("ACK_WITHOUT_ID")
            return OrderAck(
                proposal_id=proposal_id,
                broker=VENUE_T212_PRACTICE,
                broker_order_id=str(order_id),
                status="ACKNOWLEDGED",
                raw=_safe_raw(payload),
            )
        if status in _FAILED_STATUSES:
            raise _failed(f"HTTP_{status}")
        if status in _UNKNOWN_STATUSES or 500 <= status <= 599:
            raise _unknown(f"HTTP_{status}")
        raise _unknown(f"HTTP_{status}")

    # --- cancel --------------------------------------------------------------

    async def cancel(self, broker_order_id: str) -> CancelResult:
        """One DELETE for one order id. Never resent. 200 means requested, not cancelled."""

        if not self._pinned:
            return CancelResult("REFUSED", "ACCOUNT_NOT_PINNED")
        try:
            order_id = int(broker_order_id)
        except (TypeError, ValueError):
            return CancelResult("REFUSED", "ORDER_ID_INVALID")
        try:
            response = await self.transport.delete_order(order_id)
        except PracticeTransportError as exc:
            if exc.sent:
                return CancelResult("UNKNOWN", f"TRANSPORT_{exc.kind.upper()}")
            return CancelResult("REFUSED", f"NOT_SENT_{exc.kind.upper()}")
        status = response.status_code
        if status == 200:
            return CancelResult("REQUESTED", "HTTP_200")
        if status in (400, 401, 403, 404):
            return CancelResult("REFUSED", f"HTTP_{status}")
        return CancelResult("UNKNOWN", f"HTTP_{status}")


def _failed(code: str) -> BrokerExecutionError:
    error = BrokerExecutionError("Practice broker did not take the order")
    error.reason_code = code[:64]
    return error


def _unknown(code: str) -> BrokerOutcomeUnknownError:
    error = BrokerOutcomeUnknownError(
        "Practice broker outcome is unknown and requires reconciliation"
    )
    error.reason_code = code[:64]
    return error


def practice_factory(
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Callable[[VenueContext], T212PracticeDispatcher]:
    """A dispatcher factory for the seam. ``transport`` is for tests (MockTransport)."""

    def build(context: VenueContext) -> T212PracticeDispatcher:
        if context.binding is None or context.venue != VENUE_T212_PRACTICE:
            raise VenueError("VENUE_BINDING_MISSING", context.venue)
        try:
            key = workspace_credentials.uk_credential(PRACTICE_KEY_NAME)
            secret = workspace_credentials.uk_credential(PRACTICE_SECRET_NAME)
        except workspace_credentials.CredentialScopeError:
            raise VenueError("PRACTICE_CREDENTIAL_SCOPE", context.venue) from None
        if key is None or secret is None:
            # Both halves are required (HTTP Basic). There is no bare-key fallback.
            raise VenueError("PRACTICE_CREDENTIALS_MISSING", context.venue)
        http = PracticeTransport(key, secret, clock=clock, sleep=sleep, transport=transport)
        return T212PracticeDispatcher(http, context.binding, clock=clock, sleep=sleep)

    return build


__all__ = [
    "CancelResult",
    "PRACTICE_KEY_NAME",
    "PRACTICE_SECRET_NAME",
    "PracticeOrderRefused",
    "PracticePinError",
    "T212PracticeDispatcher",
    "build_limit_body",
    "practice_factory",
]
