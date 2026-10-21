"""A SHADOW world for the 63-05 Mac tests: the real Mac stack over the real VM relay app.

Only the network is faked. The Mac side is a real ``AppState.start_execution`` over a tmp
private/ directory and a tmp ledger. The VM side is the real relay app and order pipeline from
``test_gateway_orders_routes.OrderStack``, reached by a real ``RelayClient`` whose http client is
a Starlette ``TestClient`` (no socket, no tunnel). Breeze is a fake transport that records every
request, and the kill-switch metadata server is a fake that records the method of every request.

The signing key is the committed TEST ONLY vector key standing in for the Secure Enclave signer.
Nothing here touches the Keychain, a real key, a real account id or the network.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import india_limits_support as ils
from app_context import AppState
from brokers.breeze_gateway.client import RelayClient
from brokers.breeze_gateway.orders import RelayLink
from brokers.breeze_gateway.token_store import MemoryTokenStore
from execution.relay_dispatcher import RelayDispatcher
from execution.venue import VENUE_BREEZE_RELAY, production_dispatcher_factories
from market_data import IndiaInstrument, TopOfBook
from risk_india import rules
from test_gateway_orders_routes import (
    IST_OFFSET,
    PRIMARY,
    VECTORS,
    MetadataFake,
    OrderStack,
    sign,
)

SHADOW_ACCOUNT = "acct-shadow-0001"
SYMBOL = "TESTCO"
TICKER = f"NSE:CASH:{SYMBOL}"
INSTRUMENT = IndiaInstrument(symbol=SYMBOL)
ENROLL_TOKEN = b"one-time-secret-for-tests"


def shadow_private_dir(
    root: Path, *, account: str = SHADOW_ACCOUNT, capital_cap: str = "50000"
) -> Path:
    """A synthetic private/ whose India limits are the VM's limits (same nine values)."""

    private = ils.india_private_dir(
        root,
        capital_cap=capital_cap,
        per_position_cap="17500",
        collar="0.02",
        max_slippage_bps="25",
    )
    ils.write_json(
        private / "india" / "execution.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "venue": VENUE_BREEZE_RELAY,
            "account_id": account,
            "currency": "INR",
            "fat_finger_collar": "0.02",
            "max_slippage_bps": "25",
        },
    )
    return private


def replay_events(count: int = 3) -> tuple[TopOfBook, ...]:
    from datetime import timezone

    now = datetime.now(timezone.utc)
    return tuple(
        TopOfBook(
            instrument=INSTRUMENT,
            source="local-replay",
            bid="99.9",
            ask="100.0",
            observed_at=now,
            received_at=now,
            sequence=sequence,
        )
        for sequence in range(1, count + 1)
    )


class RecordingHttp:
    """Wraps the Starlette TestClient the RelayClient talks to: records, and can inject faults.

    ``timeout_after_send`` lets the request reach the VM (the VM acts on it) and then raises a
    read timeout, which is what a lost answer looks like. ``unreachable`` raises before sending.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []
        self.timeout_after_send: set[tuple[str, str]] = set()
        self.unreachable: set[tuple[str, str]] = set()

    def stream(self, method, path, **kwargs):
        key = (method, path)
        self.calls.append(key)
        if key in self.unreachable:
            raise httpx.ConnectError("unreachable")
        if key in self.timeout_after_send:
            with self._inner.stream(method, path, **kwargs) as response:
                response.read()
            raise httpx.ReadTimeout("the answer was lost")
        return self._inner.stream(method, path, **kwargs)

    def close(self) -> None:
        """The world owns the TestClient; a RelayClient closing its pool must not close it."""

    def order_calls(self) -> list[tuple[str, str]]:
        """Every request that could make the VM act on an order: the POSTs to /v1/orders/*.

        The GET reads (state, audit) are how a test looks at the VM; they change nothing there.
        """

        return [call for call in self.calls if call[1].startswith("/v1/orders/") and call[0] == "POST"]

    def count(self, method: str, path: str) -> int:
        return sum(1 for call in self.calls if call == (method, path))


class ShadowWorld:
    """The VM stack, a relay link into it, and a started Mac SHADOW AppState."""

    def __init__(
        self,
        tmp_path: Path,
        metadata: MetadataFake,
        monkeypatch,
        *,
        signer: dict = PRIMARY,
        capital_cap: str = "50000",
    ) -> None:
        self.signer = signer
        self.tmp = tmp_path
        (tmp_path / "vm").mkdir()
        (tmp_path / "mac").mkdir()
        self.vm = OrderStack(tmp_path / "vm", metadata)
        self.metadata = metadata
        # The VM's quote book carries a bid and an ask, as the Breeze quote does.
        row = self.vm.transport.quote_book[SYMBOL]
        row["best_bid_price"] = 99.9
        row["best_offer_price"] = 100.0
        self.token = self.vm.login()
        store = MemoryTokenStore()
        store.save(self.token, self.vm.clock.now_utc().astimezone(rules.IST).replace(year=2027))
        self.store = store
        self.http = RecordingHttp(self.vm.client)
        self.client = RelayClient(http=self.http, token_store=store)
        self.link = RelayLink.of(self.client)
        self.dispatcher = RelayDispatcher(self.link)
        monkeypatch.setenv("GROWIN_WORKSPACE", "india")
        self.private = shadow_private_dir(tmp_path / "mac", capital_cap=capital_cap)
        self.app = AppState()
        factories = dict(production_dispatcher_factories())
        factories[VENUE_BREEZE_RELAY] = lambda _context: self.dispatcher
        started = self.app.start_execution(
            tmp_path / "mac" / "india.sqlite3",
            workspace="india",
            private_dir=self.private,
            dispatcher_factories=factories,
            india_clock=self.mac_now,
        )
        assert started, self.app.execution_startup_error
        self.ledger = self.app._execution_ledger
        self.service = self.app.execution_service
        self.approval = self.service._approval_service

    # -- time: one clock for the VM, the Mac guard, the approval service and the quote source

    def mac_now(self) -> datetime:
        return self.vm.clock.now_utc().astimezone(rules.IST)

    # -- the signer: the TEST ONLY vector key stands in for the Secure Enclave

    def enroll_signer(self) -> None:
        path = self.approval.enrollment_token_path
        path.write_bytes(ENROLL_TOKEN)
        os.chmod(path, 0o600)
        self.approval.enroll_key(
            bytes.fromhex(self.signer["public_key_x963_hex"]), ENROLL_TOKEN, workspace="india"
        )

    def signature(self, payload: bytes, key: Optional[dict] = None) -> bytes:
        return base64.b64decode(sign(key or self.signer, payload))

    def cli_client(self) -> RelayClient:
        """A second client over the same VM, for the CLI (which closes the client it is given)."""

        return RelayClient(http=self.http, token_store=self.store)

    # -- the flow

    async def start_replay(self) -> None:
        await self.app.start_market_data_replay((INSTRUMENT,), replay_events())

    def prepare(self, quantity: str = "10", symbol: str = SYMBOL):
        """Server-side SHADOW preparation: relay quote, limit at the tick-floored LTP, admission."""

        return self.app.prepare_india_paper_local(symbol=symbol, quantity=quantity)

    def challenge(self, proposal_id: str, *, quote: Any = "fresh"):
        if quote == "fresh":
            quote = self.app.india_quote_from_relay(SYMBOL)
        return self.service.create_approval_challenge(
            proposal_id, workspace="india", india_quote=quote
        )

    async def approve(
        self, proposal_id: str, challenge, *, signature: Optional[bytes] = None, quote: Any = "fresh"
    ):
        if quote == "fresh":
            quote = self.app.india_quote_from_relay(SYMBOL)
        return await self.service.approve_signed(
            proposal_id,
            challenge.challenge_id,
            signature if signature is not None else self.signature(challenge.signed_payload),
            workspace="india",
            india_quote=quote,
        )

    def close(self) -> None:
        self.app.close_execution()


def standing_assertions(world: ShadowWorld) -> None:
    """Every test ends here: Breeze saw no non-GET, the order endpoint saw none, metadata is GET."""

    transport = world.vm.transport
    assert transport.order_endpoint_non_get() == []
    assert transport.non_get() == []
    assert set(world.metadata.methods) <= {"GET"}


__all__ = [
    "IST_OFFSET",
    "INSTRUMENT",
    "SHADOW_ACCOUNT",
    "SYMBOL",
    "TICKER",
    "VECTORS",
    "RecordingHttp",
    "ShadowWorld",
    "replay_events",
    "shadow_private_dir",
    "standing_assertions",
]
