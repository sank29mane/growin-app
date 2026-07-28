from decimal import Decimal

import pytest

from execution.models import OrderIntent, OrderState
from execution.paper_dispatcher import PaperDispatcher
from execution.service import BrokerExecutionError


def _intent(**updates: object) -> OrderIntent:
    values = {
        "proposal_id": "proposal-53-02",
        "client_order_id": "growin-proposal-53-02-v1",
        "workspace": "uk",
        "account": "invest",
        "broker": "trading212",
        "ticker": "AAPL_US_EQ",
        "side": "BUY",
        "quantity": Decimal("2.5"),
    }
    values.update(updates)
    return OrderIntent(**values)


@pytest.mark.asyncio
async def test_paper_order_id_is_stable_across_replays_and_instances():
    intent = _intent()

    first = await PaperDispatcher().dispatch(intent)
    replay = await PaperDispatcher().dispatch(intent)

    assert replay.broker_order_id == first.broker_order_id
    assert replay.raw == first.raw
    assert first.broker_order_id.startswith("paper-")


@pytest.mark.asyncio
async def test_changed_immutable_intent_identity_produces_a_different_order_id():
    original = _intent()
    changed = _intent(
        client_order_id="growin-proposal-53-02-v2",
        quantity=Decimal("3.0"),
    )

    original_ack = await PaperDispatcher().dispatch(original)
    changed_ack = await PaperDispatcher().dispatch(changed)

    assert changed_ack.broker_order_id != original_ack.broker_order_id


@pytest.mark.asyncio
async def test_paper_dispatch_acknowledges_without_reporting_a_fill():
    ack = await PaperDispatcher().dispatch(_intent())

    assert ack.broker == "paper"
    assert ack.status == OrderState.ACKNOWLEDGED.value
    assert ack.status != OrderState.FILLED.value
    assert ack.idempotent_replay is False
    assert set(ack.raw) == {
        "execution_mode",
        "identity_source",
        "identity_digest",
    }
    assert ack.raw["execution_mode"] == "paper"
    assert ack.raw["identity_source"] == "client_order_id"
    assert "AAPL_US_EQ" not in str(ack.raw)
    assert "2.5" not in str(ack.raw)


@pytest.mark.asyncio
async def test_paper_dispatcher_has_no_external_client_or_runtime_state():
    dispatcher = PaperDispatcher()

    assert vars(dispatcher) == {}
    ack = await dispatcher.dispatch(_intent())
    assert ack.status == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_paper_dispatcher_refuses_live_intent():
    with pytest.raises(BrokerExecutionError, match="refuses non-paper"):
        await PaperDispatcher().dispatch(_intent(mode="LIVE"))
