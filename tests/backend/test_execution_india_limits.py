"""The Mac's India limits on the real admission path (Phase 63-04, RISK-03 Mac side).

Tracer first: private config on disk, ``start_execution``, then an over-cap BUY denied by
admission with evidence recorded and no reservation. The rows after it exercise each rule,
the latch file and the slippage gate through ``ExecutionService.admit`` with explicit
simulator and risk evidence, so every figure is exact. Every value is synthetic, the clock is
injected, nothing contacts a broker, and the ledger and private/ directory live in tmp.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import india_limits_support as ils
from app_context import AppState
from execution.service import _json_safe
from regime_testkit import bound_admit, gated
from execution import AdmissionDecision, ExecutionLedger, ExecutionService
from execution.ledger import canonical_json
from market_data import IndiaInstrument, TopOfBook
from risk_india import rules
from risk_india.drawdown import SessionResult
from risk_india.exits import Position
from risk_india.state import STATE_FILE_NAME, state_path_for

TCS = "NSE:CASH:TCS"
TCS_ISIN = "INE467B01029"
INSTRUMENT = IndiaInstrument(symbol="RELIANCE")


def tcs_evidence(**overrides):
    return ils.make_evidence(stock_code="TCS", isin=TCS_ISIN, **overrides)


@pytest.fixture
def world(tmp_path):
    private = ils.india_private_dir(tmp_path)
    ledger = ils.open_ledger(tmp_path)
    guard = ils.make_guard(ledger, private)
    service = ils.make_service(ledger, guard)
    try:
        yield SimpleNamespace(
            private=private, ledger=ledger, guard=guard, service=service, tmp=tmp_path
        )
    finally:
        ledger.close()


def decide(world, proposal_id, **kwargs):
    """Admit (and reserve when admitted) one order built from ``kwargs``."""
    evidence = kwargs.pop("evidence", None)
    fill = kwargs.pop("fill", None)
    intent = ils.make_intent(proposal_id, **kwargs)
    return ils.prepare(world.service, intent, evidence=evidence, fill=fill)


def assert_denied(world, admission, code):
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == code
    assert world.ledger.get_reservation(admission.proposal_id) is None
    assert world.ledger.get_order(admission.proposal_id).state == "REJECTED"
    assert len(admission.evidence_hash) == 64


def lay_session(world, *, cash: str, positions=(), closes=None, session=ils.SESSION):
    result = world.guard.store.evaluate_session(
        closes or {},
        session,
        cash=Decimal(cash),
        positions=list(positions),
    )
    assert isinstance(result, SessionResult)
    return result


# ------------------------------------------------------------------ the tracer


def replay_events(count=3):
    from datetime import timezone

    now = datetime.now(timezone.utc)
    return tuple(
        TopOfBook(
            instrument=INSTRUMENT,
            source="local-replay",
            bid="99",
            ask="101",
            observed_at=now,
            received_at=now,
            sequence=sequence,
        )
        for sequence in range(1, count + 1)
    )


@pytest.mark.asyncio
async def test_tracer_over_cap_buy_is_denied_from_private_config_through_start_execution(tmp_path):
    private = ils.india_private_dir(tmp_path, capital_cap="1000.00", per_position_cap="1000.00")
    app = AppState()
    assert app.start_execution(
        tmp_path / "india.sqlite3",
        workspace="india",
        private_dir=private,
        india_clock=lambda: ils.NOW,
    )
    try:
        assert app.execution_service.india_guard is not None
        await app.start_market_data_replay((INSTRUMENT,), replay_events())
        ledger = app._execution_ledger
        ledger.configure_paper_budget("paper", "INR", "100000", workspace="india")
        ils.seed_position(ledger, TCS, 9, "900.00", guard=app.execution_service.india_guard)  # deployed 900.00 of the 1000.00 cap

        def admit(proposal_id, limit_price):
            proposal = ils.make_intent(proposal_id, limit_price=limit_price).model_dump(mode="json")
            return app.admit_india_paper_proposal(
                proposal,
                instrument=INSTRUMENT,
                portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
                quote=ils.make_evidence(),
            )

        over = admit("tracer-over", "100.01")  # 900.00 + 100.01 is one paisa over
        assert over.decision is AdmissionDecision.DENIED
        assert over.reason_code == "capital_cap"
        assert ledger.get_reservation("tracer-over") is None
        assert ledger.get_order("tracer-over").state == "REJECTED"
        denied_events = [e for e in ledger.list_events("tracer-over") if e.event_type == "ADMISSION_DECIDED"]
        assert denied_events[0].payload["reason_code"] == "capital_cap"
        assert denied_events[0].payload["evidence_hash"] == over.evidence_hash

        exact = admit("tracer-exact", "100.00")  # 900.00 + 100.00 is exactly the cap
        assert exact.decision is AdmissionDecision.ADMITTED
        assert exact.reason_code == "ADMITTED"
        assert ledger.get_reservation("tracer-exact") is not None
        assert app.execution_service.get_proposal("tracer-exact")["status"] == "PENDING"
    finally:
        await app.close_market_data()
        app.close_execution()


@pytest.mark.asyncio
async def test_tracer_per_position_cap_denies_one_paisa_over(tmp_path):
    private = ils.india_private_dir(tmp_path, capital_cap="1000.00", per_position_cap="400.00")
    app = AppState()
    assert app.start_execution(
        tmp_path / "india.sqlite3", workspace="india", private_dir=private, india_clock=lambda: ils.NOW
    )
    try:
        await app.start_market_data_replay((INSTRUMENT,), replay_events())
        app._execution_ledger.configure_paper_budget("paper", "INR", "100000", workspace="india")
        ils.seed_position(
            app._execution_ledger, ils.TICKER, 3, "300.00", guard=app.execution_service.india_guard
        )  # RELIANCE held at 300.00

        def admit(proposal_id, limit_price):
            proposal = ils.make_intent(proposal_id, limit_price=limit_price).model_dump(mode="json")
            return app.admit_india_paper_proposal(
                proposal,
                instrument=INSTRUMENT,
                portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
                quote=ils.make_evidence(),
            )

        assert admit("pp-over", "100.01").reason_code == "per_position_cap"
        assert admit("pp-exact", "100.00").decision is AdmissionDecision.ADMITTED
    finally:
        await app.close_market_data()
        app.close_execution()


def test_india_without_execution_json_does_not_start_and_leaves_no_ledger(tmp_path):
    private = ils.india_private_dir(tmp_path)
    (private / "india" / "execution.json").unlink()
    app = AppState()
    ledger_path = tmp_path / "never.sqlite3"
    assert app.start_execution(ledger_path, workspace="india", private_dir=private) is False
    assert app.execution_authority is False
    assert "EXECUTION_CONFIG_MISSING" in app.execution_startup_error
    assert not ledger_path.exists()


def test_start_execution_installs_the_guard_only_for_india(tmp_path, private_config_dir):
    india = AppState()
    uk = AppState()
    try:
        assert india.start_execution(
            tmp_path / "i.sqlite3", workspace="india", private_dir=private_config_dir
        )
        assert uk.start_execution(tmp_path / "u.sqlite3", workspace="uk", private_dir=private_config_dir)
        assert india.execution_service.india_guard is not None
        assert uk.execution_service.india_guard is None
    finally:
        india.close_execution()
        uk.close_execution()


# ---------------------------------------------------------------- caps and reservations


def test_active_reservations_count_toward_both_caps(world):
    first = decide(world, "res-a", quantity=4)  # 400.00 reserved on RELIANCE
    assert first.decision is AdmissionDecision.ADMITTED
    same_name = decide(world, "res-b", quantity=3)  # 400 + 300 = 700 over the 600 per-position cap
    assert_denied(world, same_name, "per_position_cap")
    other = decide(world, "res-c", quantity=6, ticker=TCS, evidence=tcs_evidence())  # 400 + 600 = 1000
    assert other.decision is AdmissionDecision.ADMITTED
    over = decide(world, "res-d", quantity=1, ticker=TCS, evidence=tcs_evidence())  # 1000 + 100 > cap
    assert_denied(world, over, "capital_cap")


def test_a_fully_cleared_reservation_stops_counting(world):
    first = decide(world, "rel-a", quantity=6)
    assert first.decision is AdmissionDecision.ADMITTED
    assert_denied(world, decide(world, "rel-b", quantity=1), "per_position_cap")
    world.ledger.reject("rel-a", "test")
    assert decide(world, "rel-c", quantity=6).decision is AdmissionDecision.ADMITTED


# ------------------------------------------------------------- price, session, quote rows


def test_collar_edges_are_inclusive_on_both_sides(world):
    assert decide(world, "col-hi-ok", limit_price="102.00").decision is AdmissionDecision.ADMITTED
    assert_denied(world, decide(world, "col-hi", limit_price="102.01"), "collar")
    assert_denied(world, decide(world, "col-lo", limit_price="97.99"), "collar")


def test_circuit_band_denies_inside_the_collar(world):
    narrow = ils.make_evidence(lower_circuit=Decimal("99.50"), upper_circuit=Decimal("100.50"))
    admission = decide(world, "band", limit_price="101.00", evidence=narrow)
    assert_denied(world, admission, "circuit_band")


def test_off_tick_price_is_denied_against_the_dated_reference(world):
    on_050 = ils.make_evidence(tick_reference=Decimal("250.00"))
    assert_denied(world, decide(world, "tick-bad", limit_price="100.01", evidence=on_050), "off_tick")
    assert decide(world, "tick-ok", limit_price="100.05", evidence=on_050).decision is AdmissionDecision.ADMITTED


def test_a_missing_or_stale_tick_reference_is_never_a_pass(world):
    none = ils.make_evidence(tick_reference=None, tick_reference_month=None)
    assert_denied(world, decide(world, "ref-none", evidence=none), "tick_reference_unavailable")
    stale = ils.make_evidence(tick_reference_month=date(2026, 8, 31))
    assert_denied(world, decide(world, "ref-stale", evidence=stale), "tick_reference_unavailable")


def test_session_cutoff_is_15_10_00_ist_and_weekends_are_closed(tmp_path):
    private = ils.india_private_dir(tmp_path)
    ledger = ils.open_ledger(tmp_path)
    try:
        def at(moment):
            guard = ils.make_guard(ledger, private, now=moment)
            service = ils.make_service(ledger, guard)
            session = moment.date()
            evidence = ils.make_evidence(observed_at=moment, session_date=session)
            name = f"s-{moment.strftime('%a-%H%M%S')}"
            return ils.admit(service, ils.make_intent(name), evidence=evidence)

        ist = rules.IST
        assert at(datetime(2026, 10, 8, 15, 9, 59, tzinfo=ist)).decision is AdmissionDecision.ADMITTED
        closed = at(datetime(2026, 10, 8, 15, 10, 0, tzinfo=ist))
        assert (closed.decision, closed.reason_code) == (AdmissionDecision.DENIED, "session_closed")
        before_open = at(datetime(2026, 10, 8, 9, 14, 59, tzinfo=ist))
        assert before_open.reason_code == "session_closed"
        saturday = at(datetime(2026, 10, 10, 10, 0, tzinfo=ist))
        assert saturday.reason_code == "session_closed"
    finally:
        ledger.close()


def test_a_missing_quote_is_quote_unavailable_never_a_pass(world):
    admission = ils.admit(world.service, ils.make_intent("noq"), evidence=ils.NO_QUOTE)
    assert_denied(world, admission, "quote_unavailable")


def test_a_quote_for_another_session_or_with_no_band_is_quote_unavailable(world):
    old = ils.make_evidence(session_date=date(2026, 10, 7))
    assert_denied(world, decide(world, "old-day", evidence=old), "quote_unavailable")
    no_band = ils.make_evidence(lower_circuit=Decimal("0"), upper_circuit=Decimal("0"))
    assert_denied(world, decide(world, "no-band", evidence=no_band), "quote_unavailable")


def test_a_stale_intraday_quote_is_refused_before_slippage_is_read(world):
    # 63-02 review: slippage_check has no freshness input, so admission refuses a stale quote.
    fresh_edge = ils.make_evidence(observed_at=ils.NOW - timedelta(seconds=30))
    assert decide(world, "age-30", evidence=fresh_edge).decision is AdmissionDecision.ADMITTED
    stale = ils.make_evidence(observed_at=ils.NOW - timedelta(seconds=31))
    assert_denied(world, decide(world, "age-31", evidence=stale), "quote_unavailable")
    future = ils.make_evidence(observed_at=ils.NOW + timedelta(seconds=1))
    assert_denied(world, decide(world, "age-future", evidence=future), "quote_unavailable")


def test_identity_mismatch_and_unsupported_series_are_denied(world):
    other = ils.make_evidence(stock_code="TCS", isin=TCS_ISIN)
    assert_denied(world, decide(world, "wrong-name", evidence=other), "isin_mismatch")
    bz = ils.make_evidence(series="BZ")
    assert_denied(world, decide(world, "bz", evidence=bz), "instrument_unsupported")


def test_malformed_orders_are_denied_with_mac_codes(world):
    no_limit = ils.make_intent("no-limit", limit_price=None)
    assert_denied(world, ils.admit(world.service, no_limit), "intent_invalid")
    fractional = ils.make_intent("frac", quantity="1.5")
    assert_denied(world, ils.admit(world.service, fractional), "intent_invalid")
    wrong_venue = ils.make_intent("nyse", ticker="NYSE:CASH:IBM")
    assert_denied(world, ils.admit(world.service, wrong_venue), "instrument_unsupported")


# ------------------------------------------------------------------- slippage gate


def test_buy_slippage_25_00_passes_and_25_01_is_denied(world):
    ok = decide(world, "slip-buy-ok", fill="100.25")  # (100.25 - 100.00) / 100.00 = 25.00 bps
    assert ok.decision is AdmissionDecision.ADMITTED
    bad = decide(world, "slip-buy-bad", fill="100.2501")  # 25.01 bps
    assert_denied(world, bad, "SLIPPAGE_LIMIT")


def test_sell_slippage_mirrors_the_buy(world):
    ils.seed_position(world.ledger, ils.TICKER, 10, "1000.00", guard=world.guard)
    locked = ils.make_evidence(bid=Decimal("100.00"), ask=Decimal("100.00"))
    ok = decide(world, "slip-sell-ok", side="SELL", fill="99.75", evidence=locked)  # 25.00 bps
    assert ok.decision is AdmissionDecision.ADMITTED
    bad = decide(world, "slip-sell-bad", side="SELL", fill="99.7499", evidence=locked)  # 25.01 bps
    assert bad.decision is AdmissionDecision.DENIED and bad.reason_code == "SLIPPAGE_LIMIT"
    better = decide(world, "slip-sell-better", side="SELL", fill="100.50", evidence=locked)
    assert better.decision is AdmissionDecision.ADMITTED


def test_a_side_without_its_book_price_is_never_a_pass(world):
    no_ask = ils.make_evidence(ask=None)
    assert_denied(world, decide(world, "no-ask", evidence=no_ask), "SLIPPAGE_QUOTE_UNAVAILABLE")


def test_the_slippage_cap_is_the_operators_value(tmp_path):
    private = ils.india_private_dir(tmp_path, max_slippage_bps="50")
    ledger = ils.open_ledger(tmp_path)
    try:
        service = ils.make_service(ledger, ils.make_guard(ledger, private))
        wide = ils.admit(service, ils.make_intent("wide"), fill="100.50")  # exactly 50.00 bps
        assert wide.decision is AdmissionDecision.ADMITTED
        over = ils.admit(service, ils.make_intent("wider"), fill="100.5001")
        assert over.reason_code == "SLIPPAGE_LIMIT"
    finally:
        ledger.close()


# ----------------------------------------------------------------- latches in admission


def test_halt_latch_denies_buys(world):
    lay_session(world, cash="900")  # equity 900 of a 1000 peak: -10%, past the -8% halt
    assert_denied(world, decide(world, "halted"), "halt_latch")


def test_ended_latch_denies_buys_as_pilot_ended(world):
    lay_session(world, cash="800")  # -20%, past the -15% flatten
    assert_denied(world, decide(world, "ended"), "pilot_ended")


def test_an_open_stop_denies_every_isin_until_the_exit_fill_is_recorded(world):
    ils.seed_position(world.ledger, TCS, 1, "100.00", guard=world.guard)
    stopped = lay_session(
        world,
        cash="900",
        positions=[Position(TCS, "TCS", 1, Decimal("100.00"))],
        closes={TCS: Decimal("80")},  # 80 <= 100 x 0.88: stop; equity 980, no halt
    )
    assert stopped.state.latch_names() == ("stop",)
    denied = decide(world, "other-isin")  # RELIANCE, not the stopped name
    assert_denied(world, denied, "stop_open")
    world.guard.store.apply_exit_fill(TCS, sold_quantity=1, remaining_quantity=0)
    assert decide(world, "after-fill").decision is AdmissionDecision.ADMITTED


def test_a_deleted_state_file_with_fills_denies_every_buy(world):
    assert decide(world, "first").decision is AdmissionDecision.ADMITTED  # creates the file
    state_file = state_path_for(world.ledger.path)
    assert state_file.exists() and state_file.name == STATE_FILE_NAME
    ils.seed_position(world.ledger, TCS, 1, "100.00", guard=world.guard)  # a fill exists now
    state_file.unlink()
    assert_denied(world, decide(world, "after-delete"), "state_unreadable")
    assert_denied(world, decide(world, "after-delete-2", ticker=TCS, evidence=tcs_evidence()), "state_unreadable")


def test_with_no_fills_a_missing_state_file_is_a_pilot_start(world):
    state_file = state_path_for(world.ledger.path)
    assert not state_file.exists()
    assert decide(world, "start").decision is AdmissionDecision.ADMITTED
    assert state_file.exists()


@pytest.mark.parametrize("damage", ["garbage", "hash", "workspace", "float"])
def test_a_damaged_state_file_denies_buys_with_state_unreadable(world, damage):
    assert decide(world, "creates-file").decision is AdmissionDecision.ADMITTED
    path = state_path_for(world.ledger.path)
    document = json.loads(path.read_text())
    if damage == "garbage":
        path.write_text("{not json")
    elif damage == "hash":
        document["state"]["peak"] = "999999"
        path.write_text(json.dumps(document))
    elif damage == "workspace":
        document["workspace"] = "uk"
        path.write_text(json.dumps(document))
    else:
        path.write_text(path.read_text().replace('"peak":"1000.00"', '"peak":1000.0'))
    path.chmod(0o600)
    assert_denied(world, decide(world, "damaged"), "state_unreadable")


# ----------------------------------------------------- guard presence and UK untouched


def test_a_runtime_service_without_the_guard_denies_every_india_admission(tmp_path):
    ledger = ils.open_ledger(tmp_path)
    try:
        service = ExecutionService(
            ils.PaperDispatcher(), ledger, require_runtime_preflight=True, india_guard=None, **gated(),
        )
        admission = ils.admit(service, ils.make_intent("no-guard"))
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "india_limits_unavailable"
    finally:
        ledger.close()


def test_the_guard_is_never_consulted_for_uk(tmp_path):
    private = ils.india_private_dir(tmp_path)
    scratch = ils.open_ledger(tmp_path, name="india-scratch.sqlite3")
    guard = ils.make_guard(scratch, private)
    calls = []
    guard.check_order = lambda *a, **k: calls.append(a)  # type: ignore[method-assign]
    guard.check_slippage = lambda *a, **k: calls.append(a)  # type: ignore[method-assign]
    try:
        with ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk") as uk_ledger:
            service = ExecutionService(ils.PaperDispatcher(), uk_ledger, india_guard=guard, **gated())
            intent = ils.make_intent(
                "uk-1", ticker="VUSA", workspace="uk", account="invest", limit_price=None
            )
            evidence_at = datetime.now(ils.NOW.tzinfo)
            admission = service.admit(
                intent,
                currency="GBP",
                price="100",
                simulator_evidence={"simulated_fill_price": "100"},
                risk_evidence={"scaled_size": "1"}, **bound_admit(),
                evidence_at=evidence_at,
            )
            assert admission.decision is AdmissionDecision.ADMITTED
            assert calls == []
            # The stored evidence is exactly the pre-63 UK shape: no India section.
            expected = {
                "simulator": {"simulated_fill_price": "100"},
                "risk": {"scaled_size": "1.0"},
                "evidence_at": evidence_at.isoformat(),
                "max_age_seconds": 30,
                "current_spread_pct": "0.002",
                # The regime binding the admission was made under is part of what is hashed.
                "regime": _json_safe(bound_admit()["regime_audit"]),
            }
            digest = hashlib.sha256(canonical_json(expected).encode("utf-8")).hexdigest()
            assert admission.evidence_hash == digest
            # UK still cannot admit a SELL in a paper ledger.
            sell = service.admit(
                ils.make_intent(
                    "uk-sell", ticker="VUSA", workspace="uk", account="invest", side="SELL", limit_price=None
                ),
                currency="GBP",
                price="100",
                simulator_evidence={"simulated_fill_price": "100"},
                risk_evidence={"scaled_size": "1"}, **bound_admit(),
            )
            assert sell.decision is AdmissionDecision.DENIED
            assert sell.reason_code == "SELL_ADMISSION_REQUIRES_A_POSITION_RESERVATION"
    finally:
        scratch.close()


def test_the_denial_evidence_names_every_code_the_rules_returned(world):
    # Two rules fail at once (collar and circuit band); the first is the reason, both are recorded.
    narrow = ils.make_evidence(lower_circuit=Decimal("99.50"), upper_circuit=Decimal("100.50"))
    admission = decide(world, "two-codes", limit_price="103.00", evidence=narrow)
    assert admission.reason_code == "circuit_band"
    rows = world.ledger._connection.execute(  # noqa: SLF001 - read the immutable evidence hash input
        "SELECT evidence_hash FROM execution_admissions WHERE proposal_id = 'two-codes'"
    ).fetchone()
    evidence = {
        "simulator": {"simulated_fill_price": "100.00"},
        # The India guard denied this order before the risk gate ran, so the risk block is
        # the caller's, not the gate's.
        "risk": {"scaled_size": "1"},
        "evidence_at": admission.evidence_at.isoformat(),
        "max_age_seconds": 30,
        "current_spread_pct": "0",
        "regime": _json_safe(bound_admit()["regime_audit"]),
        "india": {"guard": True, "codes": ["circuit_band", "collar"]},
    }
    assert rows["evidence_hash"] == hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()
