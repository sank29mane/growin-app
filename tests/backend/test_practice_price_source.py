"""PR #557 fix 6: ``local-replay`` is a test-only price source.

It is the fixture replay the tests record quotes with. Production must never admit
from it, so a service admits it only when a test injects it explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import t212_practice_testkit as kit
from execution import ExecutionLedger, ExecutionService, VenueBinding
from execution.venue import (
    ADMISSIBLE_PRICE_SOURCES,
    PRICE_SOURCE_OPERATOR_RECORDED,
    PRICE_SOURCE_TEST_REPLAY,
    admissible_price_sources,
)
from regime_testkit import calm_probabilities
from t212_practice_testkit import PRACTICE_ACCOUNT, practice_proposal_dict, start_practice_stack
from t212_testkit import install_no_real_network


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


@pytest.fixture(autouse=True)
def regime_zero(monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(
        regime_module, "fast_gmm_predict_proba", lambda feature, **params: calm_probabilities()
    )


def test_the_production_price_source_set_has_only_the_operator_recorded_source():
    assert ADMISSIBLE_PRICE_SOURCES == frozenset({PRICE_SOURCE_OPERATOR_RECORDED})
    assert PRICE_SOURCE_TEST_REPLAY not in admissible_price_sources()
    assert PRICE_SOURCE_TEST_REPLAY in admissible_price_sources(allow_test_sources=True)


async def production_stack(tmp_path, private_dir, monkeypatch):
    """The practice stack started exactly as production starts it (no test opt-in)."""

    original = kit.AppState.start_execution

    def start_without_opt_in(self, *args, **kwargs):
        kwargs.pop("allow_test_price_sources", None)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(kit.AppState, "start_execution", start_without_opt_in)
    stack = await start_practice_stack(tmp_path, private_dir, monkeypatch)
    assert stack.started, stack.app.execution_startup_error
    return stack


@pytest.mark.asyncio
async def test_a_production_started_practice_service_refuses_local_replay(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await production_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        assert stack.service.admissible_price_sources == frozenset({PRICE_SOURCE_OPERATOR_RECORDED})
        denied = stack.service.admit(
            practice_proposal_dict("prod-replay", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_divisor="100", price_source=PRICE_SOURCE_TEST_REPLAY,
            **stack.app._local_paper_preflight(),
        )
        assert denied.decision.value == "DENIED"
        assert denied.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
        assert stack.ledger.get_reservation("prod-replay") is None
        # The same call from the operator-recorded source is not refused for its source.
        recorded = stack.service.admit(
            practice_proposal_dict("prod-recorded", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_divisor="100", price_source=PRICE_SOURCE_OPERATOR_RECORDED,
            **stack.app._local_paper_preflight(),
        )
        assert recorded.reason_code != "PRICE_SOURCE_NOT_ADMISSIBLE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_app_pre_check_refuses_local_replay_in_production_and_admits_it_for_tests(
    tmp_path, private_config_dir, monkeypatch
):
    original = kit.AppState.start_execution
    prod = await production_stack(tmp_path / "prod", private_config_dir, monkeypatch)
    try:
        denial, kwargs = await prod.app._uk_practice_preflight(
            _intent(prod), readings=[], price_source=PRICE_SOURCE_TEST_REPLAY,
            now=datetime.now(timezone.utc),
        )
        assert denial == "PRICE_SOURCE_NOT_ADMISSIBLE" and kwargs is None
    finally:
        prod.close()

    monkeypatch.setattr(kit.AppState, "start_execution", original)
    test = await start_practice_stack(tmp_path / "test", private_config_dir, monkeypatch)
    try:
        denial, _ = await test.app._uk_practice_preflight(
            _intent(test), readings=[], price_source=PRICE_SOURCE_TEST_REPLAY,
            now=datetime.now(timezone.utc),
        )
        assert denial != "PRICE_SOURCE_NOT_ADMISSIBLE", "the injected test source passes the source check"
    finally:
        test.close()


def _intent(stack):
    return stack.service.register_proposal(practice_proposal_dict("src-1", quantity="1", limit_price="50"))


def test_a_bound_ledger_service_refuses_local_replay_unless_a_test_injects_it(tmp_path):
    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "bound.sqlite3", workspace="uk", venue=binding) as ledger:
        refusing = ExecutionService(None, ledger, simulator=None, risk_gate=None)
        injected = ExecutionService(
            None, ledger, simulator=None, risk_gate=None, allow_test_price_sources=True
        )
        evidence = {
            "simulator_evidence": {"simulated_fill_price": "0.5"},
            "risk_evidence": {"scaled_size": "1"},
        }
        denied = refusing.admit(
            practice_proposal_dict("r-1", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_divisor="100", price_source=PRICE_SOURCE_TEST_REPLAY, **evidence,
        )
        assert denied.decision.value == "DENIED"
        assert denied.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
        allowed = injected.admit(
            practice_proposal_dict("r-2", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_divisor="100", price_source=PRICE_SOURCE_TEST_REPLAY, **evidence,
        )
        assert allowed.decision.value == "ADMITTED"
