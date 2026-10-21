"""67-FU1 F4: the 25-case risk critic eval.

``test_live_*`` runs the real critic path (``RiskAgent.review``: registry role,
``run_structured``, the rewritten prompt, workspace limits) against a local model
runtime. It is skipped unless ``GROWIN_LIVE_CRITIC_BASE_URL`` and
``GROWIN_LIVE_CRITIC_MODEL`` are set, and always skipped when ``CI`` is set.

    CI= GROWIN_LIVE_CRITIC_BASE_URL=http://127.0.0.1:1234/v1 \\
    GROWIN_LIVE_CRITIC_MODEL=<model id as the runtime reports it> \\
    PYTHONPATH=backend uv run --project backend pytest \\
    tests/backend/test_critic_live_eval.py -m live_local -s -q

The critic needs a reasoning model with thinking on: the call sends no
``response_format`` (the schema is in the prompt), and a reply with no reasoning
evidence is refused (D3).

Per case it prints PASS, FALSE_APPROVE, FAIL_CLOSED, SOFT or FALSE_BLOCK. It fails on
any FALSE_APPROVE, any FAIL_CLOSED (CRITIC_OUTPUT_INVALID or MODEL_MISMATCH) and any
must_block case that is not BLOCKED. FALSE_BLOCK is reported, not asserted.

The other tests here run the same harness against the loopback stub, so the
scoring and the acceptance check are proven without any model.
"""

import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

import pytest

import model_registry_testkit as kit
from agents.risk_agent import RiskAgent, RiskAssessment
from app_context import state
from critic_cases import (
    CASES,
    FAIL_CLOSED,
    FALSE_APPROVE,
    FALSE_BLOCK,
    INFO,
    MUST_BLOCK,
    MUST_NOT_BLOCK,
    PASS,
    SOFT,
    CriticCase,
    live_eval_settings,
    score,
)
from market_context import MarketContext, PortfolioData, PriceData
from model_registry.wire_schema import wire_spec
from model_stub_server import embedded_schema
from model_registry import ModelRegistryError, ProviderError, load_registry_file, set_active_registry
from model_registry_testkit import key_env, network_guard, stub  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio


@dataclass
class CaseResult:
    case: CriticCase
    verdict: str
    status: str
    seconds: float


async def run_cases(monkeypatch, tmp_path, cases=CASES) -> List[CaseResult]:
    """Run each case through ``RiskAgent.review`` under its own workspace limits."""
    results = []
    for case in cases:
        capital, per_position = case.limits_for()
        monkeypatch.setattr(
            state, "workspace_config", kit.uk_practice_config(tmp_path / f"ws{case.case_id}", capital, per_position)
        )
        context = MarketContext(
            query=case.suggestion,
            ticker=case.ticker,
            intent=case.intent,
            price=PriceData(ticker=case.ticker or "N/A", current_price=1.0, validated=True),
            portfolio=PortfolioData(total_value=float(case.portfolio_value), cash_balance={"total": 0.0}),
            risk_governance=case.governance,
        )
        if case.wash_alert:
            context.user_context["recent_trades"] = [
                {"ticker": case.ticker, "side": "SELL", "pnl": -50.0, "timestamp": "2026-01-01"}
            ]
        started = time.time()
        try:
            review = await RiskAgent().review(context, case.suggestion)
            status: Optional[str] = review["status"]
            label = status
        except (ProviderError, ModelRegistryError) as exc:
            status, label = None, exc.code
        results.append(CaseResult(case, score(case, status), label, time.time() - started))
    return results


def report(results: List[CaseResult]) -> str:
    lines = [f"  {r.case.case_id:2} {r.case.kind:14} {r.status:24} {r.verdict:13} {r.seconds:5.1f}s" for r in results]
    counts = Counter(r.verdict for r in results)
    lines.append(f"SUMMARY {dict(counts)} total {sum(r.seconds for r in results):.0f}s")
    return "\n".join(lines)


def acceptance_failures(results: List[CaseResult]) -> List[str]:
    """Why the run does not meet the acceptance bar; empty when it does."""
    failures = []
    for r in results:
        if r.verdict == FALSE_APPROVE:
            failures.append(f"case {r.case.case_id}: FALSE_APPROVE")
        elif r.verdict == FAIL_CLOSED:
            failures.append(f"case {r.case.case_id}: FAIL_CLOSED ({r.status})")
        elif r.case.kind == MUST_BLOCK and r.status != "BLOCKED":
            failures.append(f"case {r.case.case_id}: must_block returned {r.status}")
    return failures


# --- the live eval ----------------------------------------------------------------------------------

_LIVE = live_eval_settings(os.environ)


@pytest.mark.live_local
@pytest.mark.skipif(_LIVE is None, reason="set GROWIN_LIVE_CRITIC_BASE_URL and GROWIN_LIVE_CRITIC_MODEL (never runs in CI)")
async def test_live_critic_25(tmp_path, monkeypatch):
    document = {
        "schema_version": 1,
        "providers": {
            "local": {
                "kind": "openai_compatible",
                "base_url": _LIVE["base_url"],
                "api_key_env": None,
                "timeout_s": _LIVE["timeout_s"],
            }
        },
        "roles": {
            "risk_critic": {
                "provider": "local",
                "model": _LIVE["model"],
                "temperature": 0.0,
                "max_tokens": _LIVE["max_tokens"],
            }
        },
    }
    path = kit.write_registry(tmp_path / "registry", document)
    set_active_registry(load_registry_file(path))
    try:
        results = await run_cases(monkeypatch, tmp_path)
    finally:
        set_active_registry(None)
    print("\n" + report(results))
    assert acceptance_failures(results) == []


# --- the harness, against the stub -------------------------------------------------------------------


@pytest.fixture
def stub_registry(stub, registry_factory):
    registry_factory()
    return stub


@pytest.fixture
def registry_factory(tmp_path, stub, key_env):
    def build():
        kit.activate(kit.make_registry(tmp_path / "private", stub.url))

    try:
        yield build
    finally:
        set_active_registry(None)


def _verdict(status):
    return {
        "status": status,
        "confidence_score": 0.7,
        "risk_assessment": "s",
        "compliance_notes": "n",
        "recommendation_adjustment": "none",
        "debate_refutation": "d",
        "requires_hitl": True,
    }


async def test_a_critic_that_blocks_every_must_block_case_meets_the_bar(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    results = await run_cases(monkeypatch, tmp_path)
    assert acceptance_failures(results) == []
    verdicts = Counter(r.verdict for r in results)
    assert verdicts[PASS] == 16 and verdicts[FALSE_BLOCK] == 8 and verdicts[INFO] == 1
    assert stub_registry.count == 25


async def test_a_critic_that_approves_everything_is_all_false_approves(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("APPROVED")
    results = await run_cases(monkeypatch, tmp_path)
    # Cases 4, 5 and 11 carry the system wash sale alert on a BUY or LONG: the
    # deterministic floor blocks them whatever the model says. The other 13 are the model's.
    assert Counter(r.verdict for r in results)[FALSE_APPROVE] == 13
    assert {r.case.case_id for r in results if r.case.wash_alert and r.status == "BLOCKED" and r.case.kind == MUST_BLOCK} == {4, 5, 11}
    failures = acceptance_failures(results)
    assert len(failures) == 13 and all("FALSE_APPROVE" in f for f in failures)


async def test_a_critic_that_only_flags_misses_the_bar_without_a_false_approve(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("FLAGGED")
    results = await run_cases(monkeypatch, tmp_path)
    assert Counter(r.verdict for r in results)[SOFT] == 13  # the floor lifts the 3 wash sale cases to BLOCKED
    assert len(acceptance_failures(results)) == 13


async def test_text_that_is_not_a_verdict_is_fail_closed_for_every_case(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = "```json\n" + str(_verdict("BLOCKED")) + "\n```"
    results = await run_cases(monkeypatch, tmp_path)
    assert {r.verdict for r in results} == {FAIL_CLOSED}
    assert {r.status for r in results} == {"CRITIC_OUTPUT_INVALID"}


async def test_a_critic_with_no_reasoning_evidence_is_fail_closed_for_every_case(stub_registry, monkeypatch, tmp_path):
    """67-FU2 D3: a verdict from a model that did not think is refused, even a correct BLOCKED."""
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    stub_registry.reasoning_tokens = 0
    results = await run_cases(monkeypatch, tmp_path)
    assert {r.verdict for r in results} == {FAIL_CLOSED}
    assert {r.status for r in results} == {"CRITIC_OUTPUT_INVALID"}
    assert len(acceptance_failures(results)) == 25


async def test_a_critic_cut_off_at_the_token_limit_is_fail_closed_for_every_case(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    stub_registry.finish_reason = "length"
    results = await run_cases(monkeypatch, tmp_path)
    assert {r.verdict for r in results} == {FAIL_CLOSED}
    assert {r.status for r in results} == {"CRITIC_OUTPUT_INVALID"}


async def test_every_case_request_has_no_response_format_and_the_schema_in_the_prompt(
    stub_registry, monkeypatch, tmp_path
):
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    await run_cases(monkeypatch, tmp_path, CASES[:3])
    for request in stub_registry.snapshot():
        assert "response_format" not in request.json and "tools" not in request.json
        assert embedded_schema(request.json) == wire_spec(RiskAssessment).schema


async def test_a_wrong_model_is_fail_closed_with_its_own_code(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    stub_registry.served_model = "some-other-loaded-model"
    results = await run_cases(monkeypatch, tmp_path, CASES[:3])
    assert {r.verdict for r in results} == {FAIL_CLOSED}
    assert {r.status for r in results} == {"MODEL_MISMATCH"}


async def test_each_case_runs_under_its_own_limits(stub_registry, monkeypatch, tmp_path):
    stub_registry.structured["RiskAssessment"] = _verdict("BLOCKED")
    await run_cases(monkeypatch, tmp_path, [CASES[0], CASES[11]])  # portfolios 100000 and 2000
    users = [r.json["messages"][1]["content"] for r in stub_registry.snapshot()]
    assert "Per-position cap: GBP 5000.00" in users[0]
    assert "Per-position cap: GBP 100.00" in users[1]


async def test_scoring_table():
    must_block = next(c for c in CASES if c.kind == MUST_BLOCK)
    must_not = next(c for c in CASES if c.kind == MUST_NOT_BLOCK)
    assert [score(must_block, s) for s in ("BLOCKED", "FLAGGED", "APPROVED", None)] == [PASS, SOFT, FALSE_APPROVE, FAIL_CLOSED]
    assert [score(must_not, s) for s in ("APPROVED", "FLAGGED", "BLOCKED", None)] == [PASS, PASS, FALSE_BLOCK, FAIL_CLOSED]


async def test_the_live_eval_is_skipped_in_ci_and_without_settings():
    settings = {"GROWIN_LIVE_CRITIC_BASE_URL": "http://127.0.0.1:1/v1", "GROWIN_LIVE_CRITIC_MODEL": "m"}
    assert live_eval_settings({}) is None
    assert live_eval_settings({"GROWIN_LIVE_CRITIC_MODEL": "m"}) is None
    assert live_eval_settings({"GROWIN_LIVE_CRITIC_BASE_URL": "http://127.0.0.1:1/v1"}) is None
    assert live_eval_settings({**settings, "CI": "true"}) is None
    assert live_eval_settings({**settings, "CI": "1"}) is None
    assert live_eval_settings(settings) == {
        "base_url": "http://127.0.0.1:1/v1", "model": "m", "timeout_s": 300, "max_tokens": 4096,
    }
    assert live_eval_settings({**settings, "CI": ""}) is not None


async def test_the_live_marker_is_registered(pytestconfig):
    assert any(line.startswith("live_local") for line in pytestconfig.getini("markers"))
