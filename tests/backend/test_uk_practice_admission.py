"""Phase 66-04 Task 1: UK practice BUY admitted from a recorded-quote replay.

Every quote is a typed reading the test records; nothing is fetched. Caps, prices
and the slippage cap are synthetic: the operator's real values live only in
``private/``. No test contacts a Trading 212 host (autouse guard, MockTransport).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from market_data.admission import (
    parse_max_slippage_bps,
    practice_notional,
    slippage_bps,
    slippage_denial,
)
from private_config import load_workspace_config
from t212_practice_testkit import (
    PRACTICE_ACCOUNT,
    FakeDemoBroker,
    route_client,
    start_practice_stack,
)
from t212_testkit import FakeClock, install_no_real_network
from regime_testkit import calm_probabilities
from venue_seam_testkit import SYNTH_LIMITS, practice_execution_payload, write_practice_files

PREPARE = "/api/t212-practice/preparations"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


@pytest.fixture
def regime_zero(monkeypatch):
    """The calmest regime (full size) from the real classifier code path.

    Only the model inference is replaced, and it names the calm component through the
    shipped model's severity map (raw id 3 today), never a literal raw id. The
    three-quote window check, the features, the severity map and the snapshot-bound
    evidence all still run.
    """

    import market_data.regime as regime_module

    monkeypatch.setattr(
        regime_module,
        "fast_gmm_predict_proba",
        lambda feature, **params: calm_probabilities(),
    )


def readings(bid="71.2", ask="71.3", *, count=3, end=None, step=2):
    end = end or datetime.now(timezone.utc)
    return [
        {
            "bid": None if bid is None else str(bid),
            "ask": None if ask is None else str(ask),
            "observed_at": (end - timedelta(seconds=step * (count - 1 - index))).isoformat(),
        }
        for index in range(count)
    ]


def body(**overrides):
    payload = {
        "confirmation": "PREPARE_T212_PRACTICE",
        "ticker": "VODl_EQ",
        "side": "BUY",
        "quantity": 2,
        "limit_price": "71.3",
        "readings": readings(),
    }
    payload.update(overrides)
    return payload


def execution(**overrides):
    payload = practice_execution_payload(account_id=PRACTICE_ACCOUNT, max_slippage_bps=25)
    payload.update(overrides)
    return payload


async def stack_with(tmp_path, private_dir, monkeypatch, **kwargs):
    kwargs.setdefault("execution", execution())
    stack = await start_practice_stack(tmp_path, private_dir, monkeypatch, **kwargs)
    assert stack.started, stack.app.execution_startup_error
    return stack


async def prepare(stack, monkeypatch, **overrides):
    async with route_client(stack, monkeypatch) as client:
        response = await client.post(PREPARE, json=body(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


def reservation_rows(stack) -> tuple[int, int]:
    raw = sqlite3.connect(stack.ledger.path)
    try:
        buying = raw.execute("SELECT COUNT(*) FROM buying_power_reservations").fetchone()[0]
        quantity = raw.execute("SELECT COUNT(*) FROM ledger_quantity_reservations").fetchone()[0]
        return buying, quantity
    finally:
        raw.close()


def assert_denied(stack, result, reason):
    assert result["admitted"] is False, result
    assert result["admission"]["decision"] == "DENIED"
    assert result["admission"]["reason_code"] == reason
    assert result["state"] == "REJECTED"
    assert stack.ledger.get_reservation(result["proposal_id"]) is None, "a denial reserves nothing"
    assert stack.broker.mutations == [], "admission never sends an order"


# --- admitted: the whole path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracer_three_recorded_quotes_are_admitted_reserved_in_gbp_and_server_stamped(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        result = await prepare(stack, monkeypatch)
        assert result["admitted"] is True, result
        assert result["state"] == "PENDING"
        admission = result["admission"]
        assert admission["decision"] == "ADMITTED" and admission["currency"] == "GBP"
        # 2 shares x 71.3 pence / 100, in pounds, at the limit price (D-18).
        assert Decimal(admission["notional"]) == Decimal("1.426")
        reservation = stack.ledger.get_reservation(result["proposal_id"])
        assert reservation.state == "ACTIVE" and reservation.reserved == Decimal("1.426")

        # The server, not the caller, stamped identity from the ledger binding.
        intent = stack.ledger.get_order(result["proposal_id"]).intent
        assert (intent["workspace"], intent["broker"], intent["mode"]) == ("uk", "t212_practice", "PRACTICE")
        assert intent["account"] == PRACTICE_ACCOUNT
        assert (intent["order_type"], intent["side"], intent["quantity"]) == ("LIMIT", "BUY", "2")
        assert "account" not in admission, "the account id is never echoed"
        assert stack.broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_gbx_notional_is_pence_divided_by_100_and_a_100x_error_would_breach_the_cap(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        per_position = Decimal(SYNTH_LIMITS["per_position_cap"])
        # 80 shares at 250 pence: GBP 200. Not divided, it would read as GBP 20,000.
        result = await prepare(
            stack, monkeypatch, quantity=80, limit_price="250",
            readings=readings("249.8", "250.0"),
        )
        assert result["admitted"] is True, result
        assert Decimal(result["admission"]["notional"]) == Decimal("200")
        assert 80 * 250 > per_position, "the undivided figure would breach the cap"
        assert Decimal("200") <= per_position
        # A GBP-quoted instrument is NOT divided: the same numbers breach the cap.
        gbp = await prepare(
            stack, monkeypatch, ticker="GBPXl_EQ", quantity=80, limit_price="250",
            readings=readings("249.8", "250.0"),
        )
        assert_denied(stack, gbp, "PER_POSITION_CAP_EXCEEDED")
    finally:
        stack.close()


def test_notional_helper_uses_the_divisor_and_the_limit_not_the_mid():
    assert practice_notional(Decimal(3), Decimal("71.3"), Decimal(100)) == Decimal("2.139")
    assert practice_notional(Decimal(3), Decimal("71.3"), Decimal(1)) == Decimal("213.9")


@pytest.mark.asyncio
async def test_the_cap_uses_the_limit_price_not_the_quote_mid(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """A marketable limit above a low mid must reserve at the limit (the worst case)."""

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch, execution=execution(max_slippage_bps=200))
    try:
        # mid is 71.25 but the limit is 72.5: notional follows the limit.
        result = await prepare(stack, monkeypatch, quantity=4, limit_price="72.5")
        assert result["admitted"] is True, result
        assert Decimal(result["admission"]["notional"]) == Decimal("2.9")
        assert Decimal(result["admission"]["price"]) == Decimal("0.725")
    finally:
        stack.close()


# --- denied, with a stable reason and no reservation -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2])
async def test_fewer_than_three_recorded_quotes_are_denied(
    count, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        result = await prepare(stack, monkeypatch, readings=readings(count=count))
        assert_denied(stack, result, "REGIME_WINDOW_INSUFFICIENT")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_three_copies_of_one_reading_are_not_three_readings(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        one = readings(count=1)[0]
        result = await prepare(stack, monkeypatch, readings=[one, one, one])
        assert_denied(stack, result, "QUOTE_READINGS_NOT_DISTINCT")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_quote_older_than_the_window_is_denied(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        old = datetime.now(timezone.utc) - timedelta(seconds=45)
        result = await prepare(stack, monkeypatch, readings=readings(end=old))
        assert_denied(stack, result, "STALE_SNAPSHOT")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_quote_just_inside_the_window_is_admitted(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        recent = datetime.now(timezone.utc) - timedelta(seconds=20)
        result = await prepare(stack, monkeypatch, readings=readings(end=recent))
        assert result["admitted"] is True, result
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_quote_in_the_future_is_denied(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        result = await prepare(stack, monkeypatch, readings=readings(end=future))
        assert_denied(stack, result, "FUTURE_EVENT")
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"ticker": "CLOSEDl_EQ"}, "EXCHANGE_CLOSED"),
        ({"ticker": "NOSUCH_EQ"}, "INSTRUMENT_UNKNOWN"),
        ({"ticker": "AAPL_US_EQ"}, "CURRENCY_NOT_ADMISSIBLE"),
        ({"ticker": "GBPXl_EQ", "quantity": 2000, "limit_price": "71.3"}, "QUANTITY_OVER_MAX_OPEN"),
        ({"quantity": 520}, "PER_POSITION_CAP_EXCEEDED"),
    ],
    ids=["closed-exchange", "unknown-ticker", "usd-instrument", "over-max-open", "per-position-cap"],
)
async def test_instrument_hours_size_and_cap_denials(
    overrides, reason, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        before = reservation_rows(stack)
        result = await prepare(stack, monkeypatch, **overrides)
        assert_denied(stack, result, reason)
        assert reservation_rows(stack) == before
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_held_notional_and_open_reservations_count_toward_the_per_position_cap(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        first = await prepare(stack, monkeypatch, quantity=300)  # 213.9 of 300
        assert first["admitted"] is True, first
        second = await prepare(stack, monkeypatch, quantity=150)  # +106.95 would be 320.85
        assert_denied(stack, second, "PER_POSITION_CAP_EXCEEDED")
        third = await prepare(stack, monkeypatch, quantity=100)  # +71.3 = 285.2: fits
        assert third["admitted"] is True, third
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_an_exhausted_budget_is_denied_even_when_the_position_cap_has_room(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    limits = dict(SYNTH_LIMITS, capital_cap="400.00", per_position_cap="300.00")
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch, limits=limits)
    try:
        first = await prepare(stack, monkeypatch, ticker="VODl_EQ", quantity=300)  # 213.9
        assert first["admitted"] is True, first
        second = await prepare(stack, monkeypatch, ticker="LLOYl_EQ", quantity=300)  # 213.9 > 186.1
        assert_denied(stack, second, "BUDGET_EXHAUSTED")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_unreadable_instrument_metadata_denies_instead_of_guessing(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)
    broker.get_override = lambda request: (
        __import__("httpx").Response(500, text="boom") if "/metadata/" in request.url.path else None
    )
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock)
    try:
        result = await prepare(stack, monkeypatch)
        assert_denied(stack, result, "METADATA_UNAVAILABLE")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_prepare_is_loopback_only_and_reads_nothing_for_a_remote_caller(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        before = len(stack.broker.requests)
        async with route_client(stack, monkeypatch, client=("203.0.113.5", 9000)) as client:
            response = await client.post(PREPARE, json=body())
        assert response.status_code == 403
        assert len(stack.broker.requests) == before
        assert stack.ledger.list_orders() == []
    finally:
        stack.close()


# --- the request cannot name identity or a price source ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"workspace": "india"},
        {"account": "someone-else"},
        {"broker": "paper"},
        {"mode": "PAPER"},
        {"order_type": "MARKET"},
        {"time_validity": "GOOD_TILL_CANCEL"},
        {"quantity": "2"},
        {"quantity": 1.5},
        {"quantity": 0},
        {"limit_price": "0"},
        {"limit_price": "-1"},
        {"source": "yahoo"},
        {"source": "position-current-price"},
        {"confirmation": "yes"},
        {"readings": []},
        {"side": "SHORT"},
    ],
)
async def test_the_prepare_request_refuses_identity_fields_odd_quantities_and_other_price_sources(
    extra, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(PREPARE, json=body(**extra))
        assert response.status_code == 422, extra
        assert stack.ledger.list_orders() == [] and reservation_rows(stack) == (0, 0)
    finally:
        stack.close()


# --- price source (D-02) --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["yahoo", "position-current-price", "t212-currentPrice", ""])
async def test_a_price_from_yahoo_or_position_current_price_stays_unadmitted_and_reserves_nothing(
    source, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    from market_data.admission import RecordedQuoteReading

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        before = reservation_rows(stack)
        proposal, admission = await stack.app.admit_uk_practice_proposal(
            ticker="VODl_EQ", side="BUY", quantity=2, limit_price=Decimal("71.3"),
            readings=[RecordedQuoteReading(**r) for r in readings()],
            price_source=source,
        )
        assert admission.decision.value == "DENIED"
        assert admission.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
        assert reservation_rows(stack) == before, "both reservation tables are unchanged"
        assert stack.ledger.get_order(proposal["proposal_id"]).state == "REJECTED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_execution_service_itself_refuses_a_bound_ledger_price_from_any_other_source(
    tmp_path, private_config_dir, monkeypatch
):
    from t212_practice_testkit import practice_proposal_dict

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        for source in (None, "yahoo", "position-current-price"):
            proposal = practice_proposal_dict(f"src-{source}")
            kwargs = {} if source is None else {"price_source": source}
            admission = stack.service.prepare(
                proposal, currency="GBP", price="0.5", **stack.app._local_paper_preflight(), **kwargs
            )
            assert admission.decision.value == "DENIED"
            assert admission.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
        assert reservation_rows(stack) == (0, 0)
    finally:
        stack.close()


# --- D-26 the slippage gate -------------------------------------------------------------------------


def test_slippage_is_side_adjusted_against_the_ask_for_a_buy_and_the_bid_for_a_sell():
    D = Decimal
    assert slippage_bps("BUY", D("100.25"), D("99"), D("100")) == D("25")
    assert slippage_bps("SELL", D("99.75"), D("100"), D("101")) == D("25")
    assert slippage_bps("BUY", D("80"), D("99"), D("100")) < 0, "a far limit is negative slippage"
    assert slippage_bps("SELL", D("120"), D("100"), D("101")) < 0
    assert slippage_bps("BUY", D("100"), D("99"), None) is None
    assert slippage_bps("SELL", D("100"), None, D("101")) is None


def test_the_cap_boundary_is_exact_at_the_cap_passes_and_a_fraction_above_denies():
    D = Decimal
    cap = D("25")
    assert slippage_denial("BUY", D("100.25"), D("99"), D("100"), cap) is None
    assert slippage_denial("BUY", D("100.2501"), D("99"), D("100"), cap) == "SLIPPAGE_LIMIT"
    assert slippage_denial("SELL", D("99.75"), D("100"), D("101"), cap) is None
    assert slippage_denial("SELL", D("99.7499"), D("100"), D("101"), cap) == "SLIPPAGE_LIMIT"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (25, "25"), ("25", "25"), ("12.5", "12.5"), (1, "1"),
        (None, None), (0, None), (-5, None), ("0", None), ("-3", None), ("abc", None),
        ("", None), (True, None), (False, None), ([25], None), ({"v": 25}, None),
        ("25 bps", None), ("1e3", None), ("NaN", None), ("Infinity", None),
    ],
)
def test_a_missing_or_unusable_cap_parses_to_none_which_means_deny_never_no_cap(raw, expected):
    parsed = parse_max_slippage_bps(raw)
    assert (None if parsed is None else str(parsed)) == expected


def test_the_slippage_gate_denies_when_the_cap_or_the_sides_quote_is_missing():
    D = Decimal
    assert slippage_denial("BUY", D("100"), D("99"), D("100"), None) == "SLIPPAGE_CAP_UNAVAILABLE"
    assert slippage_denial("BUY", D("100"), D("99"), None, D("25")) == "SLIPPAGE_QUOTE_UNAVAILABLE"
    assert slippage_denial("SELL", D("100"), None, D("101"), D("25")) == "SLIPPAGE_QUOTE_UNAVAILABLE"
    assert slippage_denial("BUY", D("100"), D("99"), D("0"), D("25")) == "SLIPPAGE_QUOTE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,cap,admitted",
    [
        ("71.3", 25, True),        # at the ask: zero slippage
        ("71.47825", 25, True),    # 71.3 x 1.0025: exactly 25 bp, at the cap
        ("71.47826", 25, False),   # one bp-fraction above
        ("71.6", 25, False),       # beyond the cap
        ("57.0", 25, True),        # a far limit, 20% under the bid, is negative slippage
        ("71.6", 50, True),        # a different synthetic cap moves the line
    ],
)
async def test_the_buy_slippage_gate_at_inside_and_beyond_the_cap(
    limit, cap, admitted, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(
        tmp_path, private_config_dir, monkeypatch, execution=execution(max_slippage_bps=cap)
    )
    try:
        result = await prepare(stack, monkeypatch, limit_price=limit, quantity=1)
        if admitted:
            assert result["admitted"] is True, result
            assert stack.ledger.get_reservation(result["proposal_id"]).state == "ACTIVE"
        else:
            assert_denied(stack, result, "SLIPPAGE_LIMIT")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_buy_is_measured_against_the_ask_not_the_bid_or_the_mid(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """A wide spread: a buy limit at the ask passes, though against the bid it would breach."""

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        wide = readings("98.0", "100.0")  # 2% wide
        limit = Decimal("100.0")
        assert slippage_bps("BUY", limit, Decimal("98.0"), Decimal("100.0")) == 0
        assert slippage_bps("BUY", limit, None, Decimal("98.0")) > 25, "measured against the bid it breaches"
        result = await prepare(stack, monkeypatch, limit_price="100.0", quantity=1, readings=wide)
        assert result["admitted"] is True, result
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_is_measured_against_the_bid_not_the_ask(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """The mirror of the buy test, through the pure rule that the practice SELL path calls."""

    D = Decimal
    wide_bid, wide_ask = D("98.0"), D("100.0")
    assert slippage_denial("SELL", D("98.0"), wide_bid, wide_ask, D("25")) is None
    assert slippage_bps("SELL", D("98.0"), wide_ask, None) > 25, "measured against the ask it would breach"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {},                                  # key absent
        {"max_slippage_bps": None},
        {"max_slippage_bps": "twenty-five"},
        {"max_slippage_bps": 0},
        {"max_slippage_bps": -25},
        {"max_slippage_bps": True},
        {"max_slippage_bps": [25]},
    ],
    ids=["absent", "null", "non-numeric", "zero", "negative", "boolean", "list"],
)
async def test_a_missing_or_unusable_cap_denies_every_order_and_never_blocks_start_up(
    config, tmp_path, private_config_dir, monkeypatch, regime_zero
):
    payload = practice_execution_payload(account_id=PRACTICE_ACCOUNT)
    payload.update(config)
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch, execution=payload)
    try:
        assert stack.app.execution_mode == "practice", "start-up is not blocked by the cap"
        result = await prepare(stack, monkeypatch, limit_price="57.0", quantity=1)  # a far limit
        assert_denied(stack, result, "SLIPPAGE_CAP_UNAVAILABLE")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_recorded_quote_without_the_asks_or_bids_value_is_denied(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        for bad in (readings(ask=None), readings(bid=None)):
            result = await prepare(stack, monkeypatch, readings=bad)
            assert_denied(stack, result, "SLIPPAGE_QUOTE_UNAVAILABLE")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_q1_marketable_buy_is_inside_the_cap_and_the_same_share_beyond_it_is_denied(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """D-27: one whole share, LIMIT DAY, at or just above the recorded ask, on a fourth ticker."""

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        for index, ticker in enumerate(["VODl_EQ", "LLOYl_EQ", "BARCl_EQ"]):
            far = await prepare(
                stack, monkeypatch, ticker=ticker, quantity=1, limit_price="57.0",
                readings=readings("71.2", "71.3"),
            )
            assert far["admitted"] is True, far  # a far limit passes the gate
        q1 = await prepare(stack, monkeypatch, ticker="TSCOl_EQ", quantity=1, limit_price="71.4",
                           readings=readings("71.2", "71.3"))
        assert q1["admitted"] is True, q1
        beyond = await prepare(stack, monkeypatch, ticker="HSBAl_EQ", quantity=1, limit_price="72.0",
                               readings=readings("71.2", "71.3"))
        assert_denied(stack, beyond, "SLIPPAGE_LIMIT")
    finally:
        stack.close()


# --- private config: max_slippage_bps -----------------------------------------------------------------


def _sub(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_the_private_config_loader_carries_max_slippage_bps_for_the_practice_venue(tmp_path):
    from private_config.errors import PrivateConfigError
    from conftest import build_private_config

    private_dir = build_private_config(_sub(tmp_path, "cfg"))
    for value in (25, "25", "abc", None, 0, -1, True, [1]):
        write_practice_files(
            private_dir, execution=practice_execution_payload(max_slippage_bps=value)
        )
        config = load_workspace_config(private_dir, "uk")
        assert config.execution.max_slippage_bps == value, "kept as written; admission decides"
    write_practice_files(private_dir)
    assert load_workspace_config(private_dir, "uk").execution.max_slippage_bps is None
    with pytest.raises(PrivateConfigError):
        write_practice_files(private_dir, execution=practice_execution_payload(max_slippage_bps=25.5))
        load_workspace_config(private_dir, "uk")  # a JSON float is refused by the loader


def test_a_paper_execution_file_ignores_max_slippage_bps(tmp_path):
    from conftest import build_private_config
    from venue_seam_testkit import write_json

    private_dir = build_private_config(_sub(tmp_path, "paper"))
    write_json(
        private_dir / "uk" / "execution.json",
        {"schema_version": 1, "workspace": "uk", "venue": "paper", "max_slippage_bps": 25},
    )
    config = load_workspace_config(private_dir, "uk")
    assert config.venue == "paper"


def test_the_config_repr_does_not_show_the_slippage_value(tmp_path):
    from conftest import build_private_config

    private_dir = build_private_config(_sub(tmp_path, "repr"))
    write_practice_files(private_dir, execution=practice_execution_payload(max_slippage_bps=31))
    config = load_workspace_config(private_dir, "uk")
    assert "31" not in repr(config.execution)


@pytest.mark.asyncio
async def test_the_reservation_transaction_itself_enforces_the_position_cap_and_needs_stored_limits(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """The admission pre-check is a convenience; the ledger is the authority (D-18)."""

    from execution import ApprovalConflict, ExecutionLedger, VenueBinding
    from execution.venue import PRICE_SOURCE_TEST_REPLAY
    from t212_practice_testkit import practice_proposal_dict

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        # 520 shares x 71.3p = GBP 370.76, over the 300 cap, forced past the pre-check.
        proposal = practice_proposal_dict("forced-cap", quantity="520", limit_price="71.3")
        admission = stack.service.admit(
            proposal, currency="GBP", price="0.713", price_divisor="100", price_source=PRICE_SOURCE_TEST_REPLAY,
            **stack.app._local_paper_preflight(),
        )
        assert admission.decision.value == "ADMITTED"
        with pytest.raises(ApprovalConflict, match="per-position cap"):
            stack.service.reserve("forced-cap")
        assert stack.ledger.get_reservation("forced-cap") is None
    finally:
        stack.close()

    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "nolimits.sqlite3", workspace="uk", venue=binding) as ledger:
        ledger.configure_paper_budget(PRACTICE_ACCOUNT, "GBP", "900", workspace="uk")
        from execution import ExecutionService

        service = ExecutionService(
            None, ledger, simulator=None, risk_gate=None, allow_test_price_sources=True
        )
        proposal = practice_proposal_dict("no-limits", quantity="1", limit_price="71.3")
        service.admit(
            proposal, currency="GBP", price="0.713", price_divisor="100", price_source=PRICE_SOURCE_TEST_REPLAY,
            simulator_evidence={"simulated_fill_price": "0.713"}, risk_evidence={"scaled_size": "1"},
        )
        with pytest.raises(ApprovalConflict, match="limits"):
            service.reserve("no-limits")


# --- the shipped regime model, no patched inference (GMM-REGIME-DEFECT fix) ---------------------------
#
# These tests never patch ``fast_gmm_predict_proba``. The real classifier reads the real
# artifact, the severity map orders its components, and the real risk gate sizes the order.
# Before the fix a tight, ordinary LSE quote landed on raw id 3 (the CALM component) and the
# old table sized it at 5%, so a whole share was denied. Raw id 3 is now sized by its severity
# rank (calm, full size) and the ordinary quote proceeds through every existing gate.


def swing_readings(swing, *, bid="71.2", ask="71.3", end=None, step=2):
    """Three readings whose middle mid swings by ``swing`` (log), ending on a tight 71.2 / 71.3.

    The classifier's volatility feature is the standard deviation of the window's log mid
    returns, so a swing of v gives a volatility of v. The spread stays about 0.14%, well
    inside the 5% block, so only the volatility moves the regime.
    """

    end = end or datetime.now(timezone.utc)
    base = (Decimal(bid) + Decimal(ask)) / 2
    middle = (base * Decimal(str(float(np.exp(swing))))).quantize(Decimal("0.0001"))
    half = Decimal("0.05")
    quotes = [(base - half, base + half), (middle - half, middle + half), (Decimal(bid), Decimal(ask))]
    return [
        {
            "bid": str(low),
            "ask": str(high),
            "observed_at": (end - timedelta(seconds=step * (len(quotes) - 1 - index))).isoformat(),
        }
        for index, (low, high) in enumerate(quotes)
    ]


@pytest.mark.asyncio
async def test_an_ordinary_tight_quote_is_admitted_at_full_size_with_no_patched_inference(
    tmp_path, private_config_dir, monkeypatch
):
    """The defect, fixed: the calm quote that used to be scaled to 0.05 of a share is admitted whole."""

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        result = await prepare(stack, monkeypatch, quantity=1)
        assert result["admitted"] is True, result
        admission = result["admission"]
        assert admission["decision"] == "ADMITTED" and admission["reason_code"] == "ADMITTED"
        assert Decimal(admission["risk_quantity"]) == 1, "the calm regime keeps the whole request"
        assert Decimal(admission["final_quantity"]) == 1, "no flooring or rounding is needed"
        reservation = stack.ledger.get_reservation(result["proposal_id"])
        assert reservation is not None and reservation.state == "ACTIVE"
        assert reservation.reserved == Decimal("0.713")
        assert stack.broker.mutations == [], "admission never sends an order"
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "swing,label,size",
    [(0.08, "normal", Decimal("0.5")), (0.25, "stressed", Decimal("0.1"))],
)
async def test_a_scaled_request_still_denies_with_no_reservation_and_no_dispatch(
    swing, label, size, tmp_path, private_config_dir, monkeypatch
):
    """Real inference on a more volatile window scales the request below the intent.

    A real broker is sent the whole intent quantity, so RISK_SCALED_BELOW_REQUEST stays: no
    flooring to one share, no practice exemption. A denial reserves and sends nothing.
    """

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        result = await prepare(stack, monkeypatch, quantity=2, readings=swing_readings(swing))
        assert_denied(stack, result, "RISK_SCALED_BELOW_REQUEST")
        assert Decimal(result["admission"]["risk_quantity"]) == 2 * size, label
        assert reservation_rows(stack) == (0, 0)
        assert stack.broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_one_share_request_is_not_floored_when_the_regime_scales_it(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        result = await prepare(stack, monkeypatch, quantity=1, readings=swing_readings(0.25))
        assert_denied(stack, result, "RISK_SCALED_BELOW_REQUEST")
        assert Decimal(result["admission"]["risk_quantity"]) == Decimal("0.1")
        assert reservation_rows(stack) == (0, 0)
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_spread_above_five_percent_is_still_blocked_even_for_the_calm_regime(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """The calm regime sizes at 1.0 but the 5% spread block is a separate, unchanged gate."""

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        wide = readings("68", "72")  # a 5.7% spread
        result = await prepare(stack, monkeypatch, quantity=1, limit_price="72", readings=wide)
        assert result["admitted"] is False, result
        assert Decimal(result["admission"]["risk_quantity"]) == 0
        assert reservation_rows(stack) == (0, 0)
        assert stack.broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_staleness_and_an_insufficient_window_still_deny_with_real_inference(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        old = datetime.now(timezone.utc) - timedelta(seconds=45)
        assert_denied(stack, await prepare(stack, monkeypatch, readings=readings(end=old)), "STALE_SNAPSHOT")
        short = await prepare(stack, monkeypatch, readings=readings(count=2))
        assert_denied(stack, short, "REGIME_WINDOW_INSUFFICIENT")
        assert reservation_rows(stack) == (0, 0)
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["another_models_table", "tampered_crisis_row"])
async def test_a_uk_admission_denies_when_the_size_table_is_not_the_trusted_models_policy(
    damage, tmp_path, private_config_dir, monkeypatch
):
    """The UK path passes the policy hash and audit; the service checks them and the table's
    actual rows against the loaded model's severity map. A table from another model, or one
    crisis multiplier raised to full size, denies with no reservation and no dispatch."""

    from regime_testkit import permuted_params
    from simulation.regime_severity import build_scaling_policy_connection, build_severity_map

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    original = stack.app._preflight_policy_connection
    try:
        if damage == "another_models_table":
            replacement = build_scaling_policy_connection(build_severity_map(permuted_params((1, 0, 2, 3))))
            stack.app._preflight_policy_connection = replacement
        else:
            original.execute("UPDATE scaling_policies SET scale_multiplier = 1.0 WHERE regime_id = 1")
            replacement = None
        result = await prepare(stack, monkeypatch, quantity=1)
        assert_denied(stack, result, "REGIME_POLICY_MISMATCH")
        assert Decimal(result["admission"]["risk_quantity"]) == 0
        assert reservation_rows(stack) == (0, 0)
        assert stack.broker.mutations == []
    finally:
        stack.app._preflight_policy_connection = original
        if replacement is not None:
            replacement.close()
        stack.close()


# --- pending practice proposals are listable and readable by id (loopback only) ---------------------


@pytest.mark.asyncio
async def test_pending_practice_proposals_are_listed_and_readable_by_id_over_loopback_only(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        admitted = await prepare(stack, monkeypatch)
        denied = await prepare(stack, monkeypatch, ticker="NOSUCH_EQ")
        async with route_client(stack, monkeypatch) as client:
            listing = (await client.get("/api/t212-practice/proposals")).json()["proposals"]
            assert [p["proposal_id"] for p in listing] == [admitted["proposal_id"]]
            view = listing[0]
            assert (view["mode"], view["broker"], view["order_type"], view["time_validity"]) == (
                "PRACTICE", "t212_practice", "LIMIT", "DAY",
            )
            assert view["action"] == "BUY" and view["quantity"] == "2" and view["limit_price"] == "71.3"
            assert PRACTICE_ACCOUNT not in str(view), "the account id never leaves the backend"
            one = await client.get(f"/api/t212-practice/proposals/{admitted['proposal_id']}")
            assert one.status_code == 200 and one.json()["status"] == "PENDING"
            gone = await client.get(f"/api/t212-practice/proposals/{denied['proposal_id']}")
            assert gone.status_code == 200 and gone.json()["admission"] == "DENIED"
            assert (await client.get("/api/t212-practice/proposals/nope")).status_code == 404
        async with route_client(stack, monkeypatch, client=("198.51.100.4", 1)) as remote:
            assert (await remote.get("/api/t212-practice/proposals")).status_code == 403
            assert (
                await remote.get(f"/api/t212-practice/proposals/{admitted['proposal_id']}")
            ).status_code == 403
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_cap_pre_check_is_pinned_to_the_limit_price_not_the_mid(
    tmp_path, private_config_dir, monkeypatch, regime_zero
):
    """PR #557 fix 5. A wide spread puts the mid below the limit.

    421 shares at limit 71.3 pence is GBP 300.173 against a GBP 300 cap. Valued at
    the mid (70.65) it is 297.44 and would fit. The cap is on what the order can
    cost, which is the limit.
    """

    stack = await stack_with(tmp_path, private_config_dir, monkeypatch)
    try:
        per_position = Decimal(SYNTH_LIMITS["per_position_cap"])
        bid, ask, limit, quantity = Decimal("70.0"), Decimal("71.3"), Decimal("71.3"), 421
        mid = (bid + ask) / 2
        assert limit > mid
        assert practice_notional(Decimal(quantity), mid, Decimal(100)) <= per_position, "mid would fit"
        assert practice_notional(Decimal(quantity), limit, Decimal(100)) > per_position, "limit does not"
        result = await prepare(
            stack, monkeypatch, quantity=quantity, limit_price=str(limit), readings=readings(str(bid), str(ask))
        )
        assert_denied(stack, result, "PER_POSITION_CAP_EXCEEDED")
        # One share fewer is GBP 299.46 at the limit: admitted, and recorded at the limit.
        fits = await prepare(
            stack, monkeypatch, quantity=quantity - 1, limit_price=str(limit),
            readings=readings(str(bid), str(ask)),
        )
        assert fits["admitted"] is True, fits
        assert Decimal(fits["admission"]["notional"]) == Decimal(quantity - 1) * limit / 100
    finally:
        stack.close()
