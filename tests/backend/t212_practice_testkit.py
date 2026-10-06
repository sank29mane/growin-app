"""Shared helpers for the Trading 212 practice tests (66-03, 66-04).

Nothing here touches a network. ``FakeDemoBroker`` is an ``httpx.MockTransport``
handler that behaves like a small demo account: it takes limit orders, keeps
pending and history lists, fills on command, and records every request. All
account ids, keys and prices are synthetic. The two key canaries are values the
tests then search for in logs, errors, acknowledgements and ledger rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app_context import AppState
from brokers.trading212.practice_dispatcher import practice_factory
from execution import ApprovalService, ExecutionLedger
from execution.venue import VENUE_T212_PRACTICE, production_dispatcher_factories
from t212_testkit import FakeClock
from venue_seam_testkit import (
    private_key,
    enroll,
    practice_execution_payload,
    sign,
    write_practice_files,
)

PRACTICE_ACCOUNT = "20260001"
KEY_CANARY = "canary-practice-key-7f3a91c2"
SECRET_CANARY = "canary-practice-secret-b04d55e8"
LIVE_KEY_CANARY = "canary-live-key-5c2e8d19"
LIVE_SECRET_CANARY = "canary-live-secret-91ab3f70"
CANARIES = (KEY_CANARY, SECRET_CANARY, LIVE_KEY_CANARY, LIVE_SECRET_CANARY)

DEMO_ORIGIN = "https://demo.trading212.com"
DEMO_LIMIT_URL = f"{DEMO_ORIGIN}/api/v0/equity/orders/limit"

LSE_INSTRUMENTS = [
    {"ticker": "VODl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 50000.0, "workingScheduleId": 202},
    {"ticker": "LLOYl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 50000.0, "workingScheduleId": 202},
    {"ticker": "BARCl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 50000.0, "workingScheduleId": 202},
    {"ticker": "TSCOl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 50000.0, "workingScheduleId": 202},
    {"ticker": "HSBAl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 50000.0, "workingScheduleId": 202},
    {"ticker": "GBPXl_EQ", "currencyCode": "GBP", "maxOpenQuantity": 1000.0, "workingScheduleId": 202},
    {"ticker": "AAPL_US_EQ", "currencyCode": "USD", "maxOpenQuantity": 10000.0, "workingScheduleId": 101},
    {"ticker": "CLOSEDl_EQ", "currencyCode": "GBX", "maxOpenQuantity": 5000.0, "workingScheduleId": 303},
]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _text(status: int, text: str, headers: Optional[dict[str, str]] = None) -> httpx.Response:
    return httpx.Response(status, text=text, headers=headers or {})


class FakeDemoBroker:
    """A MockTransport handler for the demo host. ``__call__`` takes an httpx.Request."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        account_id: str = PRACTICE_ACCOUNT,
        currency: str = "GBP",
    ) -> None:
        self.clock = clock
        self.account_id = account_id
        self.currency = currency
        self.requests: list[httpx.Request] = []
        self.times: list[float] = []
        self.next_id = 7_000_001
        self.pending: dict[int, dict[str, Any]] = {}
        self.orders: dict[int, dict[str, Any]] = {}
        self.fills: dict[int, list[dict[str, Any]]] = {}
        self.positions: dict[str, Decimal] = {}
        self.available: dict[str, Decimal] = {}  # quantityAvailableForTrading overrides
        self.instruments = [dict(item) for item in LSE_INSTRUMENTS]
        self.summary_override: Optional[Callable[[], httpx.Response]] = None
        self.post_override: Optional[Callable[[httpx.Request], httpx.Response]] = None
        self.delete_override: Optional[Callable[[httpx.Request], httpx.Response]] = None
        self.get_override: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None
        self.history_page_size: Optional[int] = None
        self.hidden_history: set[int] = set()
        self.cancel_removes_order = True
        self._next_fill = 880_001

    # --- the transport -----------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.times.append(self.clock.now)
        path = request.url.path
        if self.get_override is not None and request.method == "GET":
            override = self.get_override(request)
            if override is not None:
                return override
        if request.method == "GET" and path == "/api/v0/equity/account/summary":
            if self.summary_override is not None:
                return self.summary_override()
            return httpx.Response(200, json={"id": int(self.account_id), "currency": self.currency})
        if request.method == "POST" and path == "/api/v0/equity/orders/limit":
            if self.post_override is not None:
                return self.post_override(request)
            return self._create_order(json.loads(request.content))
        if request.method == "GET" and path == "/api/v0/equity/orders":
            return httpx.Response(200, json=[dict(order) for order in self.pending.values()])
        if request.method == "GET" and path.startswith("/api/v0/equity/orders/"):
            order = self.pending.get(int(path.rsplit("/", 1)[1]))
            if order is None:
                return _text(404, "Order not found")
            return httpx.Response(200, json=dict(order))
        if request.method == "DELETE" and path.startswith("/api/v0/equity/orders/"):
            if self.delete_override is not None:
                return self.delete_override(request)
            return self._cancel(int(path.rsplit("/", 1)[1]))
        if request.method == "GET" and path == "/api/v0/equity/history/orders":
            return self._history(request)
        if request.method == "GET" and path == "/api/v0/equity/positions":
            return self._positions(request)
        if request.method == "GET" and path == "/api/v0/equity/metadata/instruments":
            return httpx.Response(200, json=self.instruments)
        if request.method == "GET" and path == "/api/v0/equity/metadata/exchanges":
            return httpx.Response(200, json=self._exchanges())
        return _text(599, f"unexpected {request.method} {path}")

    # --- helpers for tests -----------------------------------------------------------

    def of(self, method: str, suffix: str) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == method and request.url.path.endswith(suffix)
        ]

    @property
    def posts(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == "POST"]

    @property
    def mutations(self) -> list[httpx.Request]:
        return [request for request in self.requests if request.method != "GET"]

    def instrument(self, ticker: str) -> dict[str, Any]:
        for item in self.instruments:
            if item["ticker"] == ticker:
                return item
        return {"ticker": ticker, "currencyCode": "GBX"}

    def _create_order(self, body: dict[str, Any]) -> httpx.Response:
        order_id = self.next_id
        self.next_id += 1
        quantity = body["quantity"]
        currency = self.instrument(body["ticker"])["currencyCode"]
        order = {
            "id": order_id,
            "type": "LIMIT",
            "strategy": "QUANTITY",
            "side": "SELL" if quantity < 0 else "BUY",
            "status": "NEW",
            "ticker": body["ticker"],
            "instrument": {"ticker": body["ticker"], "currency": currency},
            "currency": currency,
            "quantity": quantity,
            "filledQuantity": 0.0,
            "limitPrice": body["limitPrice"],
            "timeInForce": body["timeValidity"],
            "extendedHours": False,
            "initiatedFrom": "API",
            "createdAt": iso(self.clock.now),
        }
        self.pending[order_id] = order
        self.orders[order_id] = order
        self.fills[order_id] = []
        return httpx.Response(200, json=dict(order))

    def add_unseen_order(
        self, ticker: str, quantity: float, limit_price: float, *, created_at: float, initiated_from: str = "API"
    ) -> int:
        """An order the adapter never saw an answer for (for the UNKNOWN matcher)."""

        response = self._create_order(
            {"ticker": ticker, "quantity": quantity, "limitPrice": limit_price, "timeValidity": "DAY"}
        )
        order_id = response.json()["id"]
        self.pending[order_id]["createdAt"] = iso(created_at)
        self.pending[order_id]["initiatedFrom"] = initiated_from
        return order_id

    def fill(self, order_id: int, price: float, quantity: Optional[float] = None) -> None:
        order = self.orders[order_id]
        ordered = abs(order["quantity"])
        done = order["filledQuantity"]
        take = ordered - done if quantity is None else quantity
        scale = 100 if order["currency"] == "GBX" else 1
        fill = {
            "id": self._next_fill,
            "filledAt": iso(self.clock.now),
            "price": price,
            "quantity": take,
            "type": "TRADE",
            "tradingMethod": "TOTV",
            "walletImpact": {
                "currency": "GBP",
                "netValue": round(take * price / scale, 4),
                "fxRate": 1.0,
                "taxes": [],
                "realisedProfitLoss": 0.0,
            },
        }
        self._next_fill += 1
        self.fills[order_id].append(fill)
        order["filledQuantity"] = done + take
        sign = -1 if order["side"] == "SELL" else 1
        held = self.positions.get(order["ticker"], Decimal("0"))
        self.positions[order["ticker"]] = held + Decimal(str(take)) * sign
        if order["filledQuantity"] >= ordered:
            order["status"] = "FILLED"
            self.pending.pop(order_id, None)
        else:
            order["status"] = "PARTIALLY_FILLED"

    def expire(self, order_id: int) -> None:
        self.orders[order_id]["status"] = "CANCELLED"
        self.pending.pop(order_id, None)

    def _cancel(self, order_id: int) -> httpx.Response:
        order = self.pending.get(order_id)
        if order is None:
            return _text(404, "Order not found")
        if self.cancel_removes_order:
            order["status"] = "CANCELLED"
            self.pending.pop(order_id)
        else:
            order["status"] = "CANCELLING"
        return httpx.Response(200)

    def _history(self, request: httpx.Request) -> httpx.Response:
        ticker = request.url.params.get("ticker")
        items: list[dict[str, Any]] = []
        for order_id, order in sorted(self.orders.items(), reverse=True):
            if order_id in self.hidden_history or order["id"] in self.pending and not self.fills[order_id]:
                continue
            if order["status"] in ("NEW", "CANCELLING") and not self.fills[order_id]:
                continue
            if ticker and order["ticker"] != ticker:
                continue
            fills = self.fills[order_id] or [None]
            for fill in fills:
                items.append({"order": dict(order), "fill": fill})
        size = self.history_page_size
        if size is None:
            return httpx.Response(200, json={"items": items, "nextPagePath": None})
        cursor = int(request.url.params.get("cursor", "0"))
        page = items[cursor : cursor + size]
        more = cursor + size < len(items)
        next_path = (
            f"/api/v0/equity/history/orders?limit={size}&cursor={cursor + size}"
            + (f"&ticker={ticker}" if ticker else "")
            if more
            else None
        )
        return httpx.Response(200, json={"items": page, "nextPagePath": next_path})

    def _positions(self, request: httpx.Request) -> httpx.Response:
        ticker = request.url.params.get("ticker")
        body = []
        for name, quantity in self.positions.items():
            if ticker and name != ticker:
                continue
            body.append(
                {
                    "instrument": {"ticker": name, "currency": self.instrument(name)["currencyCode"]},
                    "quantity": float(quantity),
                    "quantityAvailableForTrading": float(self.available.get(name, quantity)),
                    "averagePricePaid": 70.0,
                    "currentPrice": 71.0,
                }
            )
        return httpx.Response(200, json=body)

    def _exchanges(self) -> list[dict[str, Any]]:
        now = self.clock.now
        return [
            {
                "id": 1,
                "name": "London Stock Exchange",
                "workingSchedules": [
                    {
                        "id": 202,
                        "timeEvents": [
                            {"date": iso(now - 3600), "type": "OPEN"},
                            {"date": iso(now + 3600), "type": "CLOSE"},
                        ],
                    },
                    {
                        "id": 303,
                        "timeEvents": [
                            {"date": iso(now - 7200), "type": "OPEN"},
                            {"date": iso(now - 3600), "type": "CLOSE"},
                        ],
                    },
                ],
            }
        ]


@dataclass
class PracticeStack:
    app: AppState
    broker: FakeDemoBroker
    clock: FakeClock
    key: Any
    ledger_path: Path
    private_dir: Path
    started: bool
    approval: Optional[ApprovalService] = None
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ledger(self) -> ExecutionLedger:
        return self.app._execution_ledger

    @property
    def service(self):
        return self.app.execution_service

    @property
    def adapter(self):
        return self.app.venue_adapter

    def close(self) -> None:
        self.app.close_execution()


def practice_env(
    monkeypatch,
    *,
    use_demo: Optional[str] = "false",
    practice_pair: bool = True,
    live_pair: bool = True,
    workspace: Optional[str] = "uk",
) -> None:
    """The 66-05 environment: live read pair and practice pair, TRADING212_USE_DEMO as given."""

    for name in (
        "GROWIN_WORKSPACE",
        "TRADING212_USE_DEMO",
        "TRADING212_PRACTICE_API_KEY",
        "TRADING212_PRACTICE_API_SECRET",
        "TRADING212_API_KEY",
        "TRADING212_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    if workspace is not None:
        monkeypatch.setenv("GROWIN_WORKSPACE", workspace)
    if use_demo is not None:
        monkeypatch.setenv("TRADING212_USE_DEMO", use_demo)
    if practice_pair:
        monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", KEY_CANARY)
        monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", SECRET_CANARY)
    if live_pair:
        monkeypatch.setenv("TRADING212_API_KEY", LIVE_KEY_CANARY)
        monkeypatch.setenv("TRADING212_API_SECRET", LIVE_SECRET_CANARY)


async def start_practice_stack(
    tmp_path: Path,
    private_dir: Path,
    monkeypatch,
    *,
    broker: Optional[FakeDemoBroker] = None,
    clock: Optional[FakeClock] = None,
    limits: Optional[dict[str, Any]] = None,
    execution: Optional[dict[str, Any]] = None,
    verify: bool = True,
    env: bool = True,
    ledger_name: str = "practice.sqlite3",
    handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
) -> PracticeStack:
    """Start a real AppState on a practice ledger with a MockTransport demo host."""

    if env:
        practice_env(monkeypatch)
    # The practice clock tracks real time so ledger-stamped times (claimed_at) line up.
    clock = clock or FakeClock(start=time.time())
    broker = broker or FakeDemoBroker(clock)
    write_practice_files(
        private_dir,
        execution=execution or practice_execution_payload(account_id=PRACTICE_ACCOUNT),
        limits=limits,
    )
    factories = dict(production_dispatcher_factories())
    factories[VENUE_T212_PRACTICE] = practice_factory(
        transport=httpx.MockTransport(handler or broker), clock=clock, sleep=clock.sleep
    )
    app = AppState()
    ledger_path = tmp_path / "practice-ledger" / "execution.sqlite3"
    started = app.start_execution(
        ledger_path,
        workspace="uk",
        private_dir=private_dir,
        dispatcher_factories=factories,
        allow_test_price_sources=True,  # explicit test-only opt-in to local-replay
    )
    stack = PracticeStack(
        app=app,
        broker=broker,
        clock=clock,
        key=private_key(),
        ledger_path=ledger_path,
        private_dir=private_dir,
        started=started,
    )
    if started:
        stack.approval = app.execution_service._approval_service
        enroll(stack.approval, stack.key)
        if verify:
            stack.started = await app.verify_execution_ready()
    return stack


def practice_proposal_dict(
    proposal_id: str,
    *,
    ticker: str = "VODl_EQ",
    side: str = "BUY",
    quantity: str = "2",
    limit_price: str = "50",
    account: str = PRACTICE_ACCOUNT,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "client_order_id": f"growin-{proposal_id}",
        "workspace": "uk",
        "account": account,
        "broker": VENUE_T212_PRACTICE,
        "mode": "PRACTICE",
        "ticker": ticker,
        "action": side,
        "quantity": quantity,
        "order_type": "LIMIT",
        "limit_price": limit_price,
        "status": "PENDING",
    }


def prepare_fixture(
    stack: PracticeStack,
    proposal_id: str,
    *,
    ticker: str = "VODl_EQ",
    side: str = "BUY",
    quantity: str = "2",
    limit_price: str = "50",
    price_gbp: str = "0.5",
    broker_available: Optional[str] = None,
):
    """Register, admit (fixture evidence) and reserve one practice proposal."""

    from execution.venue import PRICE_SOURCE_TEST_REPLAY

    proposal = practice_proposal_dict(
        proposal_id, ticker=ticker, side=side, quantity=quantity, limit_price=limit_price
    )
    stack.proposals[proposal_id] = proposal
    kwargs: dict[str, Any] = {}
    if broker_available is not None:
        kwargs["broker_available_quantity"] = broker_available
    admission = stack.service.prepare(
        proposal,
        currency="GBP",
        price=price_gbp,
        price_source=PRICE_SOURCE_TEST_REPLAY,
        **stack.app._local_paper_preflight(),
        **kwargs,
    )
    return admission


async def sign_and_approve(stack: PracticeStack, proposal_id: str):
    """Challenge, sign and approve one prepared proposal: the whole signed path."""

    challenge = stack.service.create_approval_challenge(proposal_id, workspace="uk")
    return await stack.service.approve_signed(
        proposal_id,
        challenge.challenge_id,
        sign(stack.key, challenge.signed_payload),
        workspace="uk",
    )


async def place(stack: PracticeStack, proposal_id: str, **kwargs):
    """Prepare and approve a proposal in one step; returns the ledger's stored ack."""

    admission = prepare_fixture(stack, proposal_id, **kwargs)
    assert admission.decision.value == "ADMITTED", admission.reason_code
    return await sign_and_approve(stack, proposal_id)


def all_ledger_text(ledger: ExecutionLedger) -> str:
    """Every ledger row as text, for canary searches."""

    import sqlite3

    raw = sqlite3.connect(ledger.path)
    try:
        chunks = []
        tables = [
            row[0]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
        for table in tables:
            for row in raw.execute(f'SELECT * FROM "{table}"').fetchall():
                chunks.append(repr(row))
        return "\n".join(chunks)
    finally:
        raw.close()


def route_client(stack: "PracticeStack", monkeypatch, *, client=("127.0.0.1", 4321)):
    """An httpx client over the practice router, with the route module bound to ``stack``."""

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from routes import t212_practice_routes

    api = FastAPI()
    api.include_router(t212_practice_routes.router)
    monkeypatch.setattr(t212_practice_routes, "state", stack.app)
    return AsyncClient(transport=ASGITransport(app=api, client=client), base_url="http://test")
