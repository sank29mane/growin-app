"""Shared helpers for the Phase 66 dispatcher-seam tests.

Every value here is a synthetic test value. Nothing contacts a broker: the
practice dispatcher is a recording double that the test registers in the
factory map, and no Trading 212 host string appears in this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from execution import OrderAck, OrderIntent
from execution.venue import (
    PRICE_SOURCE_TEST_REPLAY,
    VENUE_PAPER,
    VENUE_T212_PRACTICE,
    production_dispatcher_factories,
)

SYNTH_ACCOUNT = "acct-synthetic-0001"
SYNTH_LIMITS: dict[str, Any] = {
    "schema_version": 1,
    "workspace": "uk",
    "currency": "GBP",
    "capital_cap": "900.00",
    "per_position_cap": "300.00",
}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def practice_execution_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workspace": "uk",
        "venue": VENUE_T212_PRACTICE,
        "account_id": SYNTH_ACCOUNT,
        "currency": "GBP",
    }
    payload.update(overrides)
    return payload


def write_practice_files(
    private_dir: Path,
    *,
    execution: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> None:
    """Add UK execution.json and limits.json to a synthetic private/ directory."""

    uk = Path(private_dir) / "uk"
    write_json(uk / "execution.json", execution or practice_execution_payload())
    write_json(uk / "limits.json", limits or dict(SYNTH_LIMITS))


class RecordingDispatcher:
    """A practice dispatcher double. It records the frozen intent and acknowledges."""

    def __init__(self, *, broker_order_id: str = "practice-order-1") -> None:
        self.intents: list[OrderIntent] = []
        self.contexts: list[Any] = []
        self._broker_order_id = broker_order_id

    async def dispatch(self, intent: OrderIntent) -> OrderAck:
        self.intents.append(intent)
        return OrderAck(
            proposal_id=intent.proposal_id,
            broker=VENUE_T212_PRACTICE,
            broker_order_id=self._broker_order_id,
            status="ACKNOWLEDGED",
        )


def practice_factories(double: Any) -> dict[str, Any]:
    """A factory map holding the production entries plus a t212_practice double."""

    factories = dict(production_dispatcher_factories())

    def build(context: Any) -> Any:
        if hasattr(double, "contexts"):
            double.contexts.append(context)
        return double

    factories[VENUE_T212_PRACTICE] = build
    return factories


def practice_proposal(proposal_id: str = "practice-1", **overrides: Any) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "proposal_id": proposal_id,
        "client_order_id": f"growin-{proposal_id}",
        "workspace": "uk",
        "account": SYNTH_ACCOUNT,
        "broker": VENUE_T212_PRACTICE,
        "mode": "PRACTICE",
        "ticker": "VODl_EQ",
        "action": "BUY",
        "quantity": "2",
        "order_type": "LIMIT",
        "limit_price": "50",
        "status": "PENDING",
    }
    proposal.update(overrides)
    return proposal


def paper_proposal(proposal_id: str = "paper-1", **overrides: Any) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "proposal_id": proposal_id,
        "client_order_id": f"growin-{proposal_id}",
        "workspace": "uk",
        "account": "invest",
        "broker": VENUE_PAPER,
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "2",
        "status": "PENDING",
    }
    proposal.update(overrides)
    return proposal


def private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def public_x963(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def sign(key: ec.EllipticCurvePrivateKey, payload: bytes) -> bytes:
    return key.sign(payload, ec.ECDSA(hashes.SHA256()))


def enroll(approval: Any, key: ec.EllipticCurvePrivateKey, *, workspace: str = "uk") -> None:
    token = b"one-time-secret-for-tests"
    token_path = approval.enrollment_token_path
    token_path.write_bytes(token)
    os.chmod(token_path, 0o600)
    approval.enroll_key(public_x963(key), token, workspace=workspace)


def prepare(
    app_state: Any, proposal: dict[str, Any], *, price: str = "50", price_divisor: str = "1"
) -> Any:
    """Register, admit with fixture evidence, and reserve one proposal."""

    return app_state.execution_service.prepare(
        proposal,
        currency="GBP",
        price=price,
        price_divisor=price_divisor,
        # A bound venue admits only from a recorded-quote replay (D-02); this is
        # the fixture replay. A paper ledger ignores it.
        price_source=PRICE_SOURCE_TEST_REPLAY,
        **app_state._local_paper_preflight(),
    )
