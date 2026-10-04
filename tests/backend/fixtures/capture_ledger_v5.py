"""One-time capture of a real schema-v5 execution ledger as SQL text.

Run once, from a checkout whose ledger code is still schema v5:

    uv run --no-sync --project backend python tests/backend/fixtures/capture_ledger_v5.py

It drives the real ExecutionLedger, ApprovalService and ExecutionService in a
temporary directory with synthetic data and a throwaway P-256 key (the private
half is never written anywhere), then writes ``ledger_v5.sql`` next to this
file. It refuses to run unless ``execution.ledger.SCHEMA_VERSION == 5`` so it
can never silently produce a v6 dump.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from execution import (  # noqa: E402
    ApprovalService,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
)
from execution import ledger as ledger_module  # noqa: E402
from execution.models import ReconciliationSnapshot, ReconciliationStatus  # noqa: E402

OUTPUT_PATH = Path(__file__).resolve().with_name("ledger_v5.sql")


def _require_v5() -> None:
    if ledger_module.SCHEMA_VERSION != 5:
        sys.exit(
            "refusing to capture: execution.ledger.SCHEMA_VERSION is "
            f"{ledger_module.SCHEMA_VERSION}, expected 5. Regenerate only from a "
            "pre-Phase-58 checkout."
        )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _proposal(proposal_id: str, account: str, quantity: str) -> dict:
    return {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": account,
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": quantity,
    }


def _admit_and_reserve(
    service: ExecutionService,
    ledger: ExecutionLedger,
    proposal: dict,
    *,
    price: str,
    budget: str,
) -> None:
    service.admit(
        proposal,
        currency="GBP",
        price=price,
        simulator_evidence={"simulated_fill_price": price},
        risk_evidence={"scaled_size": proposal["quantity"]},
    )
    ledger.configure_paper_budget(proposal["account"], "GBP", budget)
    service.reserve(proposal["proposal_id"])


async def _approve(service: ExecutionService, key, proposal_id: str) -> None:
    challenge = service.create_approval_challenge(proposal_id, ttl_seconds=60)
    signature = key.sign(challenge.signed_payload, ec.ECDSA(hashes.SHA256()))
    await service.approve_signed(proposal_id, challenge.challenge_id, signature)


async def _scenario(db_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())  # throwaway, never written
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    with ExecutionLedger(db_path, workspace="uk", require_approval=True) as ledger:
        approval = ApprovalService(ledger)
        token = b"capture-one-time-token"
        token_path = approval.enrollment_token_path
        token_path.write_bytes(token)
        os.chmod(token_path, 0o600)
        approval.enroll_key(public, token)
        service = ExecutionService(
            PaperDispatcher(),
            ledger,
            require_approval=True,
            approval_service=approval,
        )

        # 1. Approved, dispatched and filled order: dispatch attempt, approval,
        #    reservation consumed, paper position.
        _admit_and_reserve(
            service, ledger, _proposal("fixture-approved", "invest", "2"), price="10", budget="1000"
        )
        await _approve(service, key, "fixture-approved")
        order = ledger.get_order("fixture-approved")
        assert order is not None and order.acknowledgment is not None
        ledger.reconcile(
            ReconciliationSnapshot(
                proposal_id="fixture-approved",
                broker_order_id=order.acknowledgment.broker_order_id,
                source="fixture-capture",
                cumulative_quantity=Decimal("2"),
                cumulative_notional=Decimal("20"),
                status=ReconciliationStatus.FILLED,
                evidence_fingerprint="fixture-fill-1",
                observed_at=datetime.now(timezone.utc),
            )
        )

        # 2. Pending order: admitted and reserved only, leaving an ACTIVE reservation.
        _admit_and_reserve(
            service, ledger, _proposal("fixture-pending", "invest", "1"), price="10", budget="1000"
        )

        # 3. Acknowledged parent with its own account and budget, plus one requote.
        _admit_and_reserve(
            service,
            ledger,
            _proposal("fixture-requote-parent", "requote-fixture", "2"),
            price="10",
            budget="1000",
        )
        await _approve(service, key, "fixture-requote-parent")
        parent = ledger.get_order("fixture-requote-parent")
        assert parent is not None and parent.acknowledgment is not None
        ledger.record_requote_intent(
            requote_id="fixture-rq-1",
            proposal_id=parent.proposal_id,
            parent_intent_hash=parent.intent_hash,
            parent_reconciliation_fingerprint=f"ack:{parent.acknowledgment.broker_order_id}",
            idempotency_key="fixture-requote-parent:snapshot-1",
            snapshot_hash="fixture-snapshot-1",
            candidate={
                "side": "BUY",
                "limit_price": "10.05",
                "lower_bound": "9.90",
                "upper_bound": "10.10",
                "policy_version": "local-paper-v1",
            },
        )

        # 4. Kill switch engaged, then cleared with a signed control challenge.
        ledger.engage_workspace_control("MANUAL_KILL")
        control = approval.create_control_challenge()
        approval.clear_workspace_control(
            control, key.sign(control.signed_payload, ec.ECDSA(hashes.SHA256()))
        )


def main() -> None:
    _require_v5()
    with tempfile.TemporaryDirectory(prefix="ledger-v5-capture-") as tmp:
        db_path = Path(tmp) / "execution.sqlite3"
        asyncio.run(_scenario(db_path))
        connection = sqlite3.connect(db_path)
        try:
            dump = list(connection.iterdump())
        finally:
            connection.close()

    header = [
        "-- Schema v5 execution ledger fixture, captured from real Phase 57 ledger code.",
        f"-- base commit: {_git('rev-parse', 'HEAD')}",
        f"-- execution code last changed in commit: {_git('rev-list', '-1', 'HEAD', '--', 'backend/execution')}",
        f"-- sqlite {sqlite3.sqlite_version}; python {platform.python_version()}",
        "-- script: tests/backend/fixtures/capture_ledger_v5.py",
        "-- synthetic data; throwaway key; regenerate only from a pre-Phase-58 checkout",
    ]
    # iterdump does not emit user_version; it goes after the dump's COMMIT.
    lines = header + dump + ["PRAGMA user_version = 5;"]
    OUTPUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({len(dump)} statements)")


if __name__ == "__main__":
    main()
