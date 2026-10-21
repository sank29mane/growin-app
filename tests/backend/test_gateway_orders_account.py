"""The VM account snapshot, ISIN binding and session evaluation (Phase 63-05, Task 2).

Unit tests for the strict Breeze read parsers, the security-master map, the settlement-lag
netting, the bars and tick-reference ports and the alert log, plus session replay through
the real relay app (the OrderStack of test_gateway_orders_routes). Breeze is a fake
transport everywhere. The ICICI response shapes under test are UNVERIFIED for NSE cash
until the 63-06 probe, so each one that the docs leave open is asserted to fail closed.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from test_gateway_orders_routes import (  # noqa: E402
    ISIN_A,
    ISIN_B,
    OrderStack,
    VECTORS,
    HttpResponse,
    breeze_body,
    code_of,
    holding_row,
    master_zip,
    metadata,  # noqa: F401  (fixture)
    seed_abc_stopped,
    stack,  # noqa: F401  (fixture)
    token,  # noqa: F401  (fixture)
    trade_row,
)

from gateway_vm.orders import limits as limits_mod  # noqa: E402
from gateway_vm.orders import master as master_mod  # noqa: E402
from gateway_vm.orders import reads  # noqa: E402
from gateway_vm.orders import sessions  # noqa: E402
from gateway_vm.orders.limits import LimitsError  # noqa: E402
from gateway_vm.orders.risk import apply_trades, initial_state  # noqa: E402
from gateway_vm.orders import sessions  # noqa: E402
from gateway_vm.orders.risk import apply_trades, initial_state  # noqa: E402
from gateway_vm.orders.alerts import ALERT_FILE, AlertLogError, FileAlertPort  # noqa: E402
from gateway_vm.orders.limits import IST  # noqa: E402
from gateway_vm.orders.risk import StopExitAlert  # noqa: E402

D = Decimal


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def master_map(rows=None) -> master_mod.MasterMap:
    from test_gateway_orders_routes import DEFAULT_MASTER

    return master_mod.parse_master(master_zip(rows if rows is not None else DEFAULT_MASTER))


# --------------------------------------------------------------- numbers


@pytest.mark.parametrize("value", [True, False, None, "", " 1", "1e3", "-1", "NaN", "Infinity", "1.", ".5", [1], {}])
def test_number_refuses_what_is_not_a_plain_decimal(value):
    with pytest.raises(reads.ReadFailed):
        reads.number(value)


def test_number_accepts_ints_decimals_and_plain_strings():
    assert reads.number(7) == D(7)
    assert reads.number(D("1.25")) == D("1.25")
    assert reads.number("100.05") == D("100.05")
    with pytest.raises(reads.ReadFailed):
        reads.number(D("NaN"))


@pytest.mark.parametrize("value", [True, -1, "-1", "1.5", "", "1e3", 1.5, None, "1" * 10])
def test_whole_refuses_what_is_not_a_non_negative_integer(value):
    with pytest.raises(reads.ReadFailed):
        reads.whole(value)


def test_whole_accepts_digit_strings_and_ints():
    assert reads.whole("20") == 20 and reads.whole(0) == 0


# -------------------------------------------------------------- holdings


def test_holdings_sum_repeated_isins_and_drop_zero_rows():
    held = reads.parse_holdings(
        [holding_row(ISIN_A, 5), holding_row(ISIN_A, 7), holding_row(ISIN_B, 0)]
    )
    assert held == {ISIN_A: 12}


@pytest.mark.parametrize(
    "success",
    [
        None,
        {},
        "x",
        [1],
        [{"stock_ISIN": ISIN_A}],
        [{"quantity": "1"}],
        [{"stock_ISIN": "INE0", "quantity": "1"}],
        [{"stock_ISIN": ISIN_A, "quantity": "1.5"}],
        [{"stock_ISIN": ISIN_A, "quantity": "-1"}],
        [{"stock_ISIN": ISIN_A, "quantity": True}],
    ],
)
def test_holdings_shapes_that_fail_closed(success):
    with pytest.raises(reads.ReadFailed):
        reads.parse_holdings(success)


# ---------------------------------------------------------------- trades


def test_trades_bind_to_the_master_and_sort_buys_before_sells_within_a_day():
    m = master_map()
    rows = [
        trade_row("s1", "TESTCO", "Sell", 5, "101", "08-Oct-2026"),
        trade_row("b1", "TESTCO", "Buy", 5, "100", "08-Oct-2026"),
        trade_row("b0", "ABC", "Buy", 2, "50", "07-Oct-2026"),
    ]
    trades = reads.parse_trades(rows, m)
    assert [t.trade_id for t in trades] == ["b0", "b1", "s1"]
    assert [t.isin for t in trades] == [ISIN_B, ISIN_A, ISIN_A]
    assert trades[0].executed_at == datetime(2026, 10, 7, tzinfo=IST)
    assert trades[1].side == "buy" and trades[2].price == D("101") and trades[2].charges == D("0")


def test_trade_without_charge_detail_keeps_charges_unknown():
    row = trade_row("b1", "TESTCO", "Buy", 5, "100", "08-Oct-2026")
    del row["brokerage_amount"], row["total_taxes"]
    assert reads.parse_trades([row], master_map())[0].charges is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(stock_code="NOPE"),  # not in the master: never guessed
        lambda r: r.update(stock_code="NIFTY"),  # an index line is not bound
        lambda r: r.update(action="Hold"),
        lambda r: r.update(quantity="0"),
        lambda r: r.update(average_cost="0"),
        lambda r: r.update(trade_date="2026-10-08"),
        lambda r: r.update(order_id="has space"),
        lambda r: r.pop("order_id"),
        lambda r: r.pop("trade_date"),
    ],
)
def test_trade_shapes_that_fail_closed(mutate):
    row = trade_row("b1", "TESTCO", "Buy", 5, "100", "08-Oct-2026")
    mutate(row)
    with pytest.raises(reads.ReadFailed):
        reads.parse_trades([row], master_map())


def test_a_repeated_order_id_fails_closed_rather_than_double_counting():
    row = trade_row("b1", "TESTCO", "Buy", 5, "100", "08-Oct-2026")
    with pytest.raises(reads.ReadFailed):
        reads.parse_trades([row, dict(row)], master_map())


# ---------------------------------------------------------- open orders


def order_row(**kw) -> dict:
    row = {
        "order_id": "w1",
        "stock_code": "TESTCO",
        "action": "Buy",
        "quantity": "10",
        "price": "100",
        "pending_quantity": "10",
        "status": "Ordered",
    }
    row.update(kw)
    return row


def test_working_orders_count_and_terminal_ones_do_not():
    orders = reads.parse_open_orders(
        [
            order_row(),
            order_row(order_id="w2", status="Executed", pending_quantity="0"),
            order_row(order_id="w3", status="Cancelled"),
            order_row(order_id="w4", status="Partially Executed", pending_quantity="4"),
            order_row(order_id="w5", status="SomethingNew"),  # unknown status: working, the cautious reading
        ],
        master_map(),
    )
    assert [(o.isin, o.side, o.quantity) for o in orders] == [(ISIN_A, "buy", 10), (ISIN_A, "buy", 4), (ISIN_A, "buy", 10)]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(price="0"),  # a working market buy has no notional to cap
        lambda r: r.update(stock_code="NOPE"),
        lambda r: r.update(action="x"),
        lambda r: r.pop("status"),
        lambda r: r.update(pending_quantity="-1"),
    ],
)
def test_open_order_shapes_that_fail_closed(mutate):
    row = order_row()
    mutate(row)
    with pytest.raises(reads.ReadFailed):
        reads.parse_open_orders([row], master_map())


# ----------------------------------------------------------------- quote


def quote_success(**kw) -> dict:
    row = {
        "stock_code": "TESTCO",
        "ltp": D("100.5"),
        "lower_circuit": D("90"),
        "upper_circuit": D("110"),
        "previous_close": D("99.8"),
        "ltt": "21-Oct-2026 15:55:21",
    }
    row.update(kw)
    return row


def test_quote_takes_the_session_date_from_ltt_and_accepts_a_one_row_list_or_object():
    one = reads.parse_quote([quote_success()], "TESTCO")
    assert one.session_date == date(2026, 10, 21) and one.ltp == D("100.5")
    assert reads.parse_quote(quote_success(), "TESTCO") == one


@pytest.mark.parametrize(
    "success",
    [
        None,
        [],
        [quote_success(), quote_success()],
        [quote_success(stock_code="OTHER")],
        [quote_success(ltp="100.5")],  # a quoted price is not the shape the smoke saw
        [quote_success(ltp=True)],
        [quote_success(ltt="2026-10-21 15:55:21")],
        [quote_success(ltt="")],
        [{k: v for k, v in quote_success().items() if k != "upper_circuit"}],
    ],
)
def test_quote_shapes_that_fail_closed(success):
    with pytest.raises(reads.ReadFailed):
        reads.parse_quote(success, "TESTCO")


# ------------------------------------------------------------------ bars


def bar(day: str, close="100.5") -> dict:
    return {"datetime": f"{day} 00:00:00", "close": D(close)}


def test_bars_by_raw_daily_close():
    assert reads.parse_bars([bar("2026-09-29", "99.5"), bar("2026-09-30", "99.8")]) == {
        date(2026, 9, 29): D("99.5"),
        date(2026, 9, 30): D("99.8"),
    }


@pytest.mark.parametrize(
    "success",
    [
        None,
        [bar("2026-09-29"), bar("2026-09-29")],  # a repeated date
        [bar("2026-09-29", "0")],
        [{"datetime": "2026-09-29 09:15:00", "close": D("1")}],  # not a daily bar
        [{"datetime": "garbage", "close": D("1")}],
        [{"datetime": "2026-13-45 00:00:00", "close": D("1")}],
        [{"datetime": "2026-09-29 00:00:00"}],
    ],
)
def test_bar_shapes_that_fail_closed(success):
    with pytest.raises(reads.ReadFailed):
        reads.parse_bars(success)


def test_a_response_at_the_row_cap_may_have_been_cut_and_is_never_complete():
    base = date(2020, 1, 1)
    rows = [bar((base + timedelta(days=i)).isoformat()) for i in range(reads.BARS_ROWS_CAP)]
    with pytest.raises(reads.ReadFailed) as err:
        reads.parse_bars(rows)
    assert err.value.reason == "bars_capped"
    assert len(reads.parse_bars(rows[:-1])) == reads.BARS_ROWS_CAP - 1


# ---------------------------------------------------------- the master


def test_master_binds_live_cash_rows_only():
    m = master_map(
        [
            ("1001", "TESTCO", "EQ", ISIN_A),
            ("1002", "ABC", "BE", ISIN_B),
            ("0", "DEAD", "EQ", "INE222C01034"),  # Token 0
            ("1003", "SERIES", "N1", "INE333D01045"),  # not EQ or BE
            ("1004", "BADISIN", "EQ", "NOTANISIN"),  # a live cash row with no usable ISIN
        ]
    )
    assert m.row("TESTCO") == master_mod.MasterRow(ISIN_A, "EQ")
    assert m.row("ABC").series == "BE"
    for code in ("DEAD", "SERIES", "BADISIN", "UNKNOWN"):
        assert m.row(code) is None
    assert m.code_for_isin(ISIN_A) == "TESTCO" and m.code_for_isin("INE222C01034") is None


def test_ambiguous_code_and_shared_isin_are_left_out_both_ways():
    m = master_map(
        [
            ("1001", "TESTCO", "EQ", ISIN_A),
            ("1002", "TESTCO", "BE", ISIN_B),  # one code, two live rows: ambiguous
            ("1003", "TWIN1", "EQ", "INE444E01056"),
            ("1004", "TWIN2", "EQ", "INE444E01056"),  # one ISIN, two codes: ambiguous
            ("1005", "OKCO", "EQ", "INE555F01067"),
        ]
    )
    assert m.row("TESTCO") is None and m.code_for_isin(ISIN_A) is None and m.code_for_isin(ISIN_B) is None
    assert m.row("TWIN1") is None and m.row("TWIN2") is None and m.code_for_isin("INE444E01056") is None
    assert m.row("OKCO") is not None and len(m) == 1


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        b"not a zip",
        b"",
        _zip({"Other.txt": b"Token,ShortName,Series,ISINCode\n1,A,EQ,INE000A01012\n"}),
        _zip({"NSEScripMaster.txt": b""}),
        _zip({"NSEScripMaster.txt": b"Token,ShortName\n1,A\n"}),  # a required column is missing
        _zip({"NSEScripMaster.txt": b"Token,ShortName,Series,ISINCode\n1,A,EQ\n"}),  # a short row
        _zip({"NSEScripMaster.txt": b"Token,ShortName,Series,ISINCode\n0,A,EQ,INE000A01012\n"}),  # no usable row
        _zip({"NSEScripMaster.txt": b"\xff\xfe\x00bad"}),
        _zip({f"f{i}.txt": b"x" for i in range(9)}),  # too many members
    ],
)
def test_a_master_that_cannot_be_read_is_a_MasterError_with_no_content(content):
    with pytest.raises(master_mod.MasterError) as err:
        master_mod.parse_master(content)
    assert "INE000A01012" not in str(err.value)


def test_master_size_cap(monkeypatch):
    monkeypatch.setattr(master_mod, "MAX_MEMBER_BYTES", 64)
    with pytest.raises(master_mod.MasterError):
        master_mod.parse_master(master_zip([("1001", "TESTCO", "EQ", ISIN_A)] * 20))


def test_master_holder_fetches_once_per_ist_day_and_never_writes_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fetched: list[int] = []
    day = {"d": date(2026, 10, 21)}

    def fetch() -> bytes:
        fetched.append(1)
        return master_zip([("1001", "TESTCO", "EQ", ISIN_A)])

    holder = master_mod.MasterHolder(fetch, lambda: day["d"])
    assert holder.current().row("TESTCO") and holder.current().row("TESTCO")
    assert len(fetched) == 1
    day["d"] = date(2026, 10, 22)
    holder.current()
    assert len(fetched) == 2
    assert list(tmp_path.iterdir()) == []


def test_master_holder_does_not_keep_a_failed_fetch():
    calls = {"n": 0}

    def fetch() -> bytes:
        calls["n"] += 1
        if calls["n"] == 1:
            raise reads.ReadFailed("upstream")
        return master_zip([("1001", "TESTCO", "EQ", ISIN_A)])

    holder = master_mod.MasterHolder(fetch, lambda: date(2026, 10, 21))
    with pytest.raises(reads.ReadFailed):
        holder.current()
    assert holder.current().row("TESTCO") is not None


# ------------------------------------------------- settlement-lag netting


class FakeReads:
    """Stands in for BreezeReads: returns canned Success values by call name."""

    def __init__(self, **by_call) -> None:
        self.by_call = by_call
        self.calls: list[tuple[str, dict]] = []

    def success(self, call: str, **kw):
        self.calls.append((call, kw))
        return self.by_call[call]


def account(today: datetime, *, holdings, trades, orders=()) -> reads.BreezeAccount:
    fake = FakeReads(demat_holdings_raw=holdings, trade_list_raw=trades, order_list_raw=list(orders))
    holder = master_mod.MasterHolder(lambda: master_zip([("1001", "TESTCO", "EQ", ISIN_A), ("1002", "ABC", "EQ", ISIN_B)]), lambda: today.date())
    built = reads.BreezeAccount(reads=fake, master=holder, clock=Clock(today))  # type: ignore[arg-type]
    built.fake = fake  # type: ignore[attr-defined]
    return built


WEDNESDAY = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)  # 10:00 IST


def test_snapshot_nets_todays_and_yesterdays_sales_off_the_lagging_holding():
    acct = account(
        WEDNESDAY,
        holdings=[holding_row(ISIN_A, 100), holding_row(ISIN_B, 20)],
        trades=[
            trade_row("b1", "TESTCO", "Buy", 100, "10", "01-Oct-2026"),
            trade_row("s1", "TESTCO", "Sell", 30, "11", "20-Oct-2026"),  # yesterday: still in demat
            trade_row("s2", "TESTCO", "Sell", 20, "11", "21-Oct-2026"),  # today
            trade_row("b2", "ABC", "Buy", 20, "50", "01-Oct-2026"),
        ],
    )
    snap = acct.snapshot()
    assert {h.isin: h.quantity for h in snap.holdings} == {ISIN_A: 50, ISIN_B: 20}
    assert all(h.cost == limits_mod.ZERO for h in snap.holdings)
    assert len(snap.trades) == 4


def test_snapshot_does_not_net_a_settled_sale():
    acct = account(
        WEDNESDAY,
        holdings=[holding_row(ISIN_A, 60)],
        trades=[trade_row("s1", "TESTCO", "Sell", 40, "11", "19-Oct-2026")],  # two days ago: already out of demat
    )
    assert {h.isin: h.quantity for h in acct.snapshot().holdings} == {ISIN_A: 60}


def test_snapshot_over_netting_floors_at_zero_and_never_goes_negative():
    acct = account(WEDNESDAY, holdings=[holding_row(ISIN_A, 10)], trades=[trade_row("s1", "TESTCO", "Sell", 99, "11", "21-Oct-2026")])
    assert acct.snapshot().holdings == ()


def test_monday_nets_friday_sales_not_sunday():
    monday = datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc)
    acct = account(
        monday,
        holdings=[holding_row(ISIN_A, 10)],
        trades=[trade_row("s1", "TESTCO", "Sell", 4, "11", "16-Oct-2026")],  # the Friday before
    )
    assert {h.isin: h.quantity for h in acct.snapshot().holdings} == {ISIN_A: 6}


def test_snapshot_asks_for_a_year_of_trades_and_todays_orders_only():
    acct = account(WEDNESDAY, holdings=[], trades=[])
    acct.snapshot()
    calls = dict(acct.fake.calls)
    assert calls["trade_list_raw"]["from_iso"] == "2025-10-21T00:00:00.000Z"
    assert calls["trade_list_raw"]["to_iso"] == "2026-10-21T23:59:59.000Z"
    assert calls["order_list_raw"]["from_iso"] == "2026-10-21T00:00:00.000Z"
    assert [c for c, _ in acct.fake.calls] == ["demat_holdings_raw", "trade_list_raw", "order_list_raw"]


# ----------------------------------------------- bars and the tick reference


class BarsFake:
    """BreezeReads stand-in for daily bars: serves a {date: close} book, recording each request."""

    def __init__(self, book: dict[date, float]) -> None:
        self.book = book
        self.calls: list[tuple[str, str]] = []

    def success(self, call: str, **kw):
        assert call == "daily_bars_v2_raw"
        first, last = kw["from_iso"][:10], kw["to_iso"][:10]
        self.calls.append((first, last))
        return [
            {"datetime": f"{d.isoformat()} 00:00:00", "close": D(str(c))}
            for d, c in sorted(self.book.items())
            if first <= d.isoformat() <= last
        ]


def bars_for(book) -> tuple[reads.BreezeBars, BarsFake]:
    fake = BarsFake(book)
    holder = master_mod.MasterHolder(lambda: master_zip([("1001", "TESTCO", "EQ", ISIN_A)]), lambda: date(2026, 10, 21))
    return reads.BreezeBars(reads=fake, master=holder), fake  # type: ignore[arg-type]


def test_bars_are_requested_in_chunks_that_stay_under_the_cap():
    port, fake = bars_for({date(2024, 1, 2): 10.0, date(2025, 6, 2): 11.0, date(2026, 10, 1): 12.0})
    closes = port.closes(ISIN_A, date(2024, 1, 1), date(2026, 10, 20))
    assert set(closes) == {date(2024, 1, 2), date(2025, 6, 2), date(2026, 10, 1)}
    assert len(fake.calls) == 3  # 1024 days: three chunks of at most 365
    assert fake.calls[0][0] == "2024-01-01" and fake.calls[-1][1] == "2026-10-20"
    for first, last in fake.calls:
        assert (date.fromisoformat(last) - date.fromisoformat(first)).days <= 364


def test_bars_for_an_isin_the_master_cannot_bind_fail_closed():
    port, _ = bars_for({})
    with pytest.raises(reads.ReadFailed) as err:
        port.closes(ISIN_B, date(2026, 10, 1), date(2026, 10, 2))
    assert err.value.reason == "unbound_isin"


def test_tick_reference_is_the_last_close_of_the_previous_month():
    port, _ = bars_for({date(2026, 9, 29): 99.0, date(2026, 9, 30): 99.8, date(2026, 10, 1): 105.0})
    ref = reads.BarsTickReference(bars=port).band_reference(ISIN_A, date(2026, 10, 21))
    assert (ref.price, ref.month) == (D("99.8"), date(2026, 9, 30))


def test_tick_reference_uses_the_last_trading_day_when_the_month_ends_on_a_weekend():
    port, _ = bars_for({date(2026, 5, 29): 50.0})  # 30 and 31 May 2026 are a weekend
    ref = reads.BarsTickReference(bars=port).band_reference(ISIN_A, date(2026, 6, 15))
    assert (ref.price, ref.month) == (D("50.0"), date(2026, 5, 29))


def test_tick_reference_in_january_reads_december():
    port, fake = bars_for({date(2025, 12, 31): 70.0})
    ref = reads.BarsTickReference(bars=port).band_reference(ISIN_A, date(2026, 1, 12))
    assert ref.month == date(2025, 12, 31) and fake.calls[0][0] == "2025-12-01"


def test_tick_reference_with_no_bar_in_the_month_fails_closed():
    port, _ = bars_for({})
    with pytest.raises(reads.ReadFailed):
        reads.BarsTickReference(bars=port).band_reference(ISIN_A, date(2026, 10, 21))


# ------------------------------------------------- session replay (HTTP)


def test_a_bar_that_disagrees_with_the_quotes_previous_close_is_never_evaluated(stack, token):
    seed_abc_stopped(stack)
    stack.transport.bar_book["ABC"][date(2026, 10, 20)] = 300.0  # the quote's previous_close is 440
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    state = stack.state(token)
    assert state["last_evaluated_session"] == "2026-10-19"  # progress to the last good session is kept
    assert "stop" in state["latches"]  # the stop from 7 Oct stays
    assert Decimal(state["drawdown"]) > Decimal("-0.08")  # the 300 mark never reached the ledger


def test_a_held_isin_with_no_bar_for_a_session_stops_the_replay_at_the_last_good_one(stack, token):
    seed_abc_stopped(stack)
    t = stack.transport
    t.holdings = HttpResponse(200, {}, breeze_body([holding_row(ISIN_B, 20), holding_row(ISIN_A, 10)]))
    t.trades = HttpResponse(
        200,
        {},
        breeze_body(
            [
                trade_row("ord-buy-1", "ABC", "Buy", 20, 500, "06-Oct-2026"),
                trade_row("ord-buy-2", "TESTCO", "Buy", 10, 100, "06-Oct-2026"),
            ]
        ),
    )
    t.bar_book["TESTCO"] = {date(2026, 9, 30): 99.8}
    day = date(2026, 10, 6)
    while day <= date(2026, 10, 19):  # TESTCO has no bar for 20 Oct
        if day.weekday() < 5:
            t.bar_book["TESTCO"][day] = 100.0
        day += timedelta(days=1)
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.state(token)["last_evaluated_session"] == "2026-10-19"


def test_a_capped_bars_response_refuses_every_order(stack, token, monkeypatch):
    seed_abc_stopped(stack)
    monkeypatch.setattr(reads, "BARS_ROWS_CAP", 5)  # the 11 ABC bars now look like a cut response
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.state(token)["last_evaluated_session"] is None


def test_no_holdings_and_no_trades_means_no_bars_are_read_for_evaluation(stack, token):
    assert stack.mint(token).status_code == 200
    # Only TESTCO's September close (the tick reference) was fetched.
    (request,) = stack.transport.bars_requests()
    assert "stock_code=TESTCO" in request[1] and "from_date=2026-09-01" in request[1]
    assert stack.state(token)["last_evaluated_session"] is None


def test_replay_is_resumable_and_does_not_re_evaluate_a_session(stack, token):
    seed_abc_stopped(stack)
    assert code_of(stack.mint(token))[2] == "stop_open"
    first = stack.state(token)
    assert code_of(stack.mint(token, intent_id="intent-test-0002"))[2] == "stop_open"
    assert stack.state(token) == first  # nothing moved: the same sessions are not applied twice


def test_bars_that_never_arrive_for_a_held_isin_refuse_and_evaluate_nothing(stack, token):
    """The calendar used to come only from the bars returned, so no bars meant no sessions."""
    seed_abc_stopped(stack)
    stack.transport.bar_book["ABC"] = {date(2026, 9, 30): 500.0}  # only the tick-reference month
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    state = stack.state(token)
    assert state["last_evaluated_session"] is None and state["latches"] == []
    assert code_of(stack.mint(token, intent_id="intent-test-0002")) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")


def test_bars_that_stop_early_refuse_and_keep_only_the_verified_prefix(stack, token):
    seed_abc_stopped(stack)
    bars = stack.transport.bar_book["ABC"]
    for day in [d for d in bars if d > date(2026, 10, 14)]:
        del bars[day]  # the vendor answers through 14 Oct and nothing after
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.state(token)["last_evaluated_session"] == "2026-10-14"


def test_a_weekday_with_no_close_is_a_gap_and_stops_the_replay_before_it(stack, token):
    seed_abc_stopped(stack)
    del stack.transport.bar_book["ABC"][date(2026, 10, 12)]  # a Monday inside the range
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.state(token)["last_evaluated_session"] == "2026-10-09"


def test_the_same_gap_is_complete_when_the_holiday_table_lists_the_day(tmp_path, metadata):
    stack = OrderStack(tmp_path, metadata, holidays=frozenset({date(2026, 10, 12)}))
    token = stack.login()
    seed_abc_stopped(stack)
    del stack.transport.bar_book["ABC"][date(2026, 10, 12)]
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "stop_open")
    assert stack.state(token)["last_evaluated_session"] == "2026-10-20"
    assert stack.transport.non_get() == [] and set(metadata.methods) <= {"GET"}


def test_expected_sessions_are_the_weekdays_less_the_listed_holidays():
    days = sessions.expected_sessions(date(2026, 10, 9), date(2026, 10, 14), {date(2026, 10, 13)})
    assert days == [date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 14)]  # no 10 and 11 Oct, no 13 Oct
    assert sessions.expected_sessions(date(2026, 10, 10), date(2026, 10, 11), ()) == []
    assert sessions.expected_sessions(date(2026, 10, 14), date(2026, 10, 9), ()) == []
    with pytest.raises(TypeError):
        sessions.expected_sessions(datetime(2026, 10, 9), date(2026, 10, 14), ())


def test_the_packaged_holiday_table_loads_and_is_marked_unpopulated():
    assert sessions.load_holidays() == frozenset()
    assert "UNPOPULATED" in sessions.HOLIDAY_TABLE_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        '{"schema":"other","holidays":[]}',
        '{"schema":"growin.gateway.nse_trading_holidays/1"}',
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":"2026-10-02"}',
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":["2026-13-40"]}',
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":["2026-10-03"]}',  # a Saturday
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":["2026-10-02","2026-10-02"]}',
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":[20261002]}',
        '{"schema":"growin.gateway.nse_trading_holidays/1","holidays":[],"holidays":[]}',
    ],
)
def test_a_malformed_holiday_table_is_refused(tmp_path, text):
    path = tmp_path / "holidays.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(LimitsError):
        sessions.load_holidays(path)
    with pytest.raises(LimitsError):
        sessions.load_holidays(tmp_path / "missing.json")


# ------------------------------------------- a known order id that grows


def test_a_growing_partial_fill_latches_account_mismatch_instead_of_keeping_the_first_quantity(stack, token):
    """One share at 100, then the same order id at 175 while the demat holding still lags."""
    t = stack.transport
    t.trades = HttpResponse(200, {}, breeze_body([trade_row("ord-partial", "TESTCO", "Buy", 1, 100, "21-Oct-2026")]))
    assert stack.verify_ok(token).status_code == 200
    t.trades = HttpResponse(200, {}, breeze_body([trade_row("ord-partial", "TESTCO", "Buy", 175, 100, "21-Oct-2026")]))
    # A second buy that the stale one-share ledger would have let through (175 x 100 is over the cap).
    assert code_of(stack.mint(token, intent_id="intent-test-0002", quantity=10, limit_price="100.05")) == (
        423,
        "ORDERS_BLOCKED",
        "account_mismatch",
    )
    assert "account_mismatch" in stack.state(token)["latches"]
    stack.restart()  # the latch is durable and nothing here clears it
    assert code_of(stack.mint(token, intent_id="intent-test-0003")) == (423, "ORDERS_BLOCKED", "account_mismatch")


def _trade(trade_id="t1", isin=ISIN_A, side="buy", quantity=1, price="100"):
    return limits_mod.Trade(trade_id, isin, side, quantity, D(price), D("0"))


@pytest.mark.parametrize(
    "changed",
    [
        {"quantity": 175},  # grew
        {"quantity": 1, "side": "sell"},  # flipped
        {"isin": ISIN_B},  # rebound
    ],
    ids=["grew", "side", "isin"],
)
def test_apply_trades_latches_when_a_known_order_id_comes_back_changed(changed):
    state = initial_state(_limits())
    apply_trades(state, [_trade(quantity=5)])
    assert state.account_mismatch is False
    fields = {"quantity": 5, **changed}
    assert apply_trades(state, [_trade(**fields)]) is True
    assert state.account_mismatch is True
    assert state.fills["t1"].quantity == 5  # the ledger itself keeps what it explained


def test_apply_trades_shrunk_fill_latches_and_an_identical_repeat_does_not():
    state = initial_state(_limits())
    apply_trades(state, [_trade(quantity=5)])
    assert apply_trades(state, [_trade(quantity=5)]) is False
    assert apply_trades(state, [_trade(quantity=5, price="101")]) is False  # price alone cannot bypass a cap
    assert state.account_mismatch is False
    apply_trades(state, [_trade(quantity=3)])
    assert state.account_mismatch is True


def _limits():
    return limits_mod.Limits.from_fields(VECTORS["limits"])


# ------------------------------------------------------------- alert log


def alert() -> StopExitAlert:
    return StopExitAlert(isin=ISIN_B, session_date=date(2026, 10, 21), quantity=20)


def test_alert_log_appends_private_lines_and_counts_them(tmp_path):
    port = FileAlertPort(tmp_path)
    assert port.count() == 0
    port.send(alert())
    port.send(alert())
    path = tmp_path / ALERT_FILE
    assert port.count() == 2
    assert os.lstat(path).st_mode & 0o777 == 0o600
    first = path.read_text(encoding="ascii").splitlines()[0]
    assert first == '{"isin":"%s","kind":"stop_exit_open","quantity":20,"session_date":"2026-10-21"}' % ISIN_B


def test_alert_log_refuses_a_symlink_or_a_group_readable_file(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x")
    (tmp_path / ALERT_FILE).symlink_to(target)
    with pytest.raises(AlertLogError):
        FileAlertPort(tmp_path).send(alert())
    with pytest.raises(AlertLogError):
        FileAlertPort(tmp_path).count()
    assert target.read_text() == "x"
    (tmp_path / ALERT_FILE).unlink()
    loose = tmp_path / ALERT_FILE
    loose.write_text("")
    os.chmod(loose, 0o644)
    with pytest.raises(AlertLogError):
        FileAlertPort(tmp_path).send(alert())
    assert loose.read_text() == ""


def test_alert_log_in_a_missing_directory_raises(tmp_path):
    with pytest.raises(AlertLogError):
        FileAlertPort(tmp_path / "nope").send(alert())


def test_an_unreadable_alert_log_makes_the_state_route_503(stack, token):
    target = stack.tmp / "elsewhere"
    target.write_text("x")
    (stack.state_dir / ALERT_FILE).symlink_to(target)
    assert code_of(stack.get("/v1/orders/state", token)) == (503, "ORDERS_UNAVAILABLE", "state_unreadable")
