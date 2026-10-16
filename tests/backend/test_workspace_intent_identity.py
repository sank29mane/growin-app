"""Intents, proposals and admission carry no identity defaults (ISO-01).

No test here calls the paper budget, position or pending-reservation methods.
"""

import json
import sqlite3
from decimal import Decimal

import pytest
from pydantic import ValidationError

from execution import (
    ExecutionConflictError,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    Workspace,
)
from execution.ledger import intent_hash
from execution.models import WORKSPACE_CURRENCY, OrderIntent
from ledger_fixture_support import replay_v5_fixture


def _intent_values(**overrides):
    values = {
        "proposal_id": "p-1",
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "ticker": "VUSA",
        "side": "BUY",
        "quantity": Decimal("2"),
    }
    values.update(overrides)
    return values


def _proposal(**overrides):
    proposal = {
        "proposal_id": "p-1",
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "2",
    }
    proposal.update(overrides)
    return proposal


def _count(path, table="order_intents"):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


# --- OrderIntent has no identity defaults ---


@pytest.mark.parametrize("missing", ["workspace", "account", "broker"])
def test_order_intent_requires_each_identity_field(missing):
    values = _intent_values()
    del values[missing]

    with pytest.raises(ValidationError) as raised:
        OrderIntent(**values)

    assert missing in str(raised.value)


def test_order_intent_workspace_is_a_closed_set():
    with pytest.raises(ValidationError):
        OrderIntent(**_intent_values(workspace="us"))
    with pytest.raises(ValidationError):
        OrderIntent(**_intent_values(workspace=""))

    india = OrderIntent(**_intent_values(workspace="india"))
    assert india.workspace is Workspace.INDIA
    assert india.model_dump(mode="json")["workspace"] == "india"


def test_workspace_currency_table_is_read_only():
    assert WORKSPACE_CURRENCY[Workspace.UK] == "GBP"
    assert WORKSPACE_CURRENCY[Workspace.INDIA] == "INR"
    with pytest.raises(TypeError):
        WORKSPACE_CURRENCY[Workspace.UK] = "USD"  # type: ignore[index]


# --- dict proposals ---


@pytest.mark.parametrize("missing", ["workspace", "account", "broker"])
def test_register_proposal_names_the_missing_key_and_writes_nothing(tmp_path, missing):
    proposal = _proposal()
    del proposal[missing]
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        service = ExecutionService(ledger=ledger)

        with pytest.raises(ValueError, match=f"missing required field '{missing}'"):
            service.register_proposal(proposal)

        assert _count(ledger.path) == 0


def test_get_proposal_returns_the_stored_identity(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        service = ExecutionService(ledger=ledger)
        service.register_proposal(
            _proposal(account="invest-2", broker="paper")
        )

        stored = service.get_proposal("p-1")

        assert stored["workspace"] == "uk"
        assert stored["account"] == "invest-2"
        assert stored["broker"] == "paper"


# --- decision 4: stored hashes do not move ---


def test_every_v5_fixture_intent_still_hashes_to_its_stored_hash(tmp_path):
    path = replay_v5_fixture(tmp_path / "execution.sqlite3")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT proposal_id, intent_hash, canonical_json FROM order_intents"
        ).fetchall()
    finally:
        connection.close()

    checked = 0
    for row in rows:
        rebuilt = OrderIntent.model_validate(json.loads(row["canonical_json"]))
        assert intent_hash(rebuilt) == row["intent_hash"], row["proposal_id"]
        checked += 1

    assert checked > 0
    assert checked == len(rows)


# --- admission currency ---


def _admit(service, proposal, currency, **extra):
    return service.admit(
        proposal,
        currency=currency,
        price="10",
        simulator_evidence={"simulated_fill_price": "10"},
        risk_evidence={"scaled_size": "1"},
        **extra,
    )


def test_admit_without_currency_is_a_type_error(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)

        with pytest.raises(TypeError):
            service.admit(
                _proposal(),
                price="10",
                simulator_evidence={"simulated_fill_price": "10"},
                risk_evidence={"scaled_size": "1"},
            )


@pytest.mark.parametrize(
    ("workspace", "wrong_currency"), [("uk", "INR"), ("india", "GBP"), ("uk", "USD")]
)
def test_admit_refuses_a_currency_from_another_workspace(
    tmp_path, workspace, wrong_currency
):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace=workspace) as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)

        with pytest.raises(ExecutionConflictError, match="does not match workspace"):
            _admit(service, _proposal(workspace=workspace), wrong_currency)

        assert _count(ledger.path) == 0
        assert _count(ledger.path, "execution_admissions") == 0


@pytest.mark.parametrize(("workspace", "currency"), [("uk", "GBP"), ("india", "INR")])
def test_admit_proceeds_with_the_workspace_currency(tmp_path, workspace, currency):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace=workspace) as ledger:
        guard = None
        proposal = _proposal(workspace=workspace)
        extra = {}
        if workspace == "india":
            # 63-04: India admission needs the Mac's India limits, a LIMIT order and a quote.
            import india_limits_support as ils

            (tmp_path / "cfg").mkdir()
            guard = ils.make_guard(ledger, ils.india_private_dir(tmp_path / "cfg"))
            proposal.update(
                {"ticker": ils.TICKER, "order_type": "LIMIT", "limit_price": "100.00"}
            )
            extra = {"india_quote": ils.make_evidence()}
        service = ExecutionService(PaperDispatcher(), ledger, india_guard=guard)

        admission = _admit(service, proposal, currency, **extra)

        assert admission.decision.value == "ADMITTED"
        assert admission.currency == currency
        assert _count(ledger.path) == 1
