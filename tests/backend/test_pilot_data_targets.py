"""Target universe: Nifty 500 plus liquid ETFs mapped to Breeze stock codes by ISIN, and the CLI."""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from market_data.models import is_valid_isin
from pilot_data.bhavcopy import ingest_udiff
from pilot_data.constituents import NIFTY500_URL, SMALLCAP250_URL, ingest_index_list
from pilot_data.core import PilotDataError, SourceDescriptor
from pilot_data.security_master import ingest_security_master
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import ASM_URL, GSM_URL
from pilot_data.targets import (
    ETF_LIQUIDITY_LOOKBACK,
    ETF_MIN_MEDIAN_TRADED_VALUE,
    build_target_universe,
    latest_target_universe,
    main,
)

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
AS_OF = date(2025, 3, 31)
MASTER_DATE = date(2025, 3, 1)


def isin_with_prefix(prefix: str, n: int) -> str:
    body = f"{prefix}{n:08d}"
    for digit in range(10):
        if is_valid_isin(body + str(digit)):
            return body + str(digit)
    raise AssertionError("no check digit")


def stock_isin(n: int) -> str:
    return isin_with_prefix("INE", n)


def etf_isin(n: int) -> str:
    return isin_with_prefix("INF", n)


def desc(kind: str, source="local_file", locator="fixture") -> SourceDescriptor:
    return SourceDescriptor(source=source, kind=kind, locator=locator, fetched_at=FETCHED)


def trading_dates(count: int, end: date = AS_OF) -> list[date]:
    out, day = [], end
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


# ETF scenarios: name -> (isin, per-date value pattern builder over the 60 dates)
ETFS = {
    "ETFPASS": (etf_isin(1), lambda i: "50000000.00"),
    "ETFJUST": (etf_isin(2), lambda i: "49999999.99"),
    "ETF39": (etf_isin(3), lambda i: "90000000.00" if i < 39 else None),
    "ETFMIX": (etf_isin(4), lambda i: "90000000.00" if i < 25 else ("10000000.00" if i < 40 else None)),
}


def populate(store, *, days=60, with_lists=True, with_master=True):
    master_rows = []
    list_members = []
    for n in range(1, 501):
        isin = stock_isin(n)
        symbol = f"SYM{n}"
        master_rows.append(kit.master_row(1000 + n, f"S{n}", "EQ", f"Company {n}", "0.01", "10", isin, symbol))
        list_members.append({"Company Name": f"Company {n}", "Industry": "Industry", "Symbol": symbol,
                             "Series": "EQ", "ISIN Code": isin})
    master_rows.pop(0)  # member 1 is absent from the master: no_breeze_mapping
    master_rows[0] = dict(master_rows[0], Token="0")  # member 2: dead token: no_live_token
    master_rows[1] = dict(master_rows[1], ExchangeCode="OTHERSYM")  # member 3: symbol differs from list
    for index, (name, (isin, _)) in enumerate(ETFS.items()):
        master_rows.append(kit.master_row(9000 + index, name, "EQ", f"{name} ETF", "0.01", "10", isin, name))
    master_rows.append(kit.master_row(0, "DEADETF", "EQ", "Dead ETF", "0.01", "10", etf_isin(9), "DEADETF"))
    if with_master:
        ingest_security_master(store, desc("security_master"), kit.security_master_bytes(master_rows),
                               snapshot_date=MASTER_DATE, snapshot_date_basis="test_fixture")
    if with_lists:
        ingest_index_list(store, desc("index_list_nifty500", "nse_archive"),
                          kit.index_list_csv(list_members + [kit.DUMMY_INDEX_ROW]), list_name="nifty500")
    for index, day in enumerate(trading_dates(days)):
        rows = [kit.udiff_row("SYM9", "EQ", stock_isin(500 + 9), "10", "11", "9", "10", trade_date=day)]
        for name, (isin, pattern) in ETFS.items():
            value = pattern(index)
            if value is not None:
                rows.append(kit.udiff_row(name, "EQ", isin, "100", "101", "99", "100", trade_date=day, value=value))
        ingest_udiff(
            store, desc("udiff_cm", "nse_archive", f"https://nsearchives.nseindia.com/udiff/{day}"),
            kit.udiff_zip(day, rows), trade_date=day,
        )


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


@pytest.fixture
def built(store):
    populate(store)
    return build_target_universe(store, as_of=AS_OF, workspace="india")


def test_every_nifty_isin_with_a_live_eq_row_maps_by_isin_to_its_shortname(built):
    nifty = [m for m in built.members if m.kind == "nifty500"]
    assert len(nifty) == 498
    by_isin = {m.anchor_isin: m for m in nifty}
    assert by_isin[stock_isin(10)].stock_code == "S10" and by_isin[stock_isin(10)].token == 1010
    assert all(m.anchor_series == "EQ" for m in nifty)


def test_exclusions_explain_every_unmapped_name(built):
    reasons = {e.isin: e.reason for e in built.exclusions}
    assert reasons == {stock_isin(1): "no_breeze_mapping", stock_isin(2): "no_live_token"}


def test_symbol_mismatch_still_maps_by_isin_with_a_note(built):
    member = next(m for m in built.members if m.anchor_isin == stock_isin(3))
    assert member.stock_code == "S3" and member.nse_symbol == "SYM3" and "symbol_mismatch" in member.notes


def test_etf_liquidity_boundary_and_d7_observation_rules(built):
    etfs = {m.nse_symbol: m for m in built.members if m.kind == "liquid_etf"}
    assert set(etfs) == {"ETFPASS"}
    assert "known_dates=60" in etfs["ETFPASS"].notes
    rejected = {r.nse_symbol: r for r in built.etf_rejected}
    assert set(rejected) == {"ETFJUST", "ETF39", "ETFMIX"}
    assert (rejected["ETFJUST"].reason, rejected["ETFJUST"].eligibility_median) == (
        "adv_below_min", Decimal("49999999.99"))
    assert rejected["ETF39"].reason == "insufficient_known_observations" and rejected["ETF39"].known_dates == 39
    assert rejected["ETF39"].known_median == Decimal("90000000")
    mix = rejected["ETFMIX"]
    # eligibility counts the 20 no-row dates as zero, so the median is 10M; known_median is only reported
    assert mix.reason == "adv_below_min"
    assert mix.eligibility_median == Decimal("10000000") and mix.known_median == Decimal("90000000")
    assert mix.known_dates == 40
    assert ETF_MIN_MEDIAN_TRADED_VALUE == Decimal("50000000")


def test_dead_token_etfs_are_not_candidates(built):
    assert "DEADETF" not in {m.nse_symbol for m in built.members} | {r.nse_symbol for r in built.etf_rejected}


def test_members_are_sorted_and_the_hash_is_stable(store, built):
    keys = [(m.kind, m.anchor_isin) for m in built.members]
    assert keys == sorted(keys)
    again = build_target_universe(store, as_of=AS_OF, workspace="india")
    assert again.target_sha256 == built.target_sha256 and len(built.target_sha256) == 64
    assert store.query("SELECT count(*) FROM target_universe_snapshots")[0][0] == 1
    assert latest_target_universe(store, workspace="india").target_sha256 == built.target_sha256


def test_result_carries_survivorship_and_hindsight_caveats(built):
    assert [c.code for c in built.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]


def test_preconditions_fail_closed(tmp_path):
    with PilotDataStore(tmp_path / "a", workspace="india") as store:
        populate(store, days=59)
        with pytest.raises(PilotDataError) as thin:
            build_target_universe(store, as_of=AS_OF, workspace="india")
        assert thin.value.code == "insufficient_bhavcopy_history"
    with PilotDataStore(tmp_path / "b", workspace="india") as store:
        populate(store, with_lists=False)
        with pytest.raises(PilotDataError) as no_list:
            build_target_universe(store, as_of=AS_OF, workspace="india")
        assert no_list.value.code == "index_list_missing"
    with PilotDataStore(tmp_path / "c", workspace="india") as store:
        populate(store, with_master=False)
        with pytest.raises(PilotDataError) as no_master:
            build_target_universe(store, as_of=AS_OF, workspace="india")
        assert no_master.value.code == "security_master_missing"
    with PilotDataStore(tmp_path / "d", workspace="india") as store:
        populate(store)
        with pytest.raises(PilotDataError) as early:
            build_target_universe(store, as_of=date(2025, 2, 1), workspace="india")
        assert early.value.code == "security_master_missing"


def test_lookback_constant_matches_the_plan():
    assert ETF_LIQUIDITY_LOOKBACK == 60


# --------------------------------------------------------------------- CLI
def run(capsys, *args, client=None):
    code = main([*args], client=client)
    return code, json.loads(capsys.readouterr().out)


def mock_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url), 404)
        return httpx.Response(200, content=route) if isinstance(route, bytes) else httpx.Response(route, content=b"x")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_cli_ingest_master_fetch_lists_fetch_surveillance_and_build(tmp_path, capsys):
    root = tmp_path / "root"
    master_file = tmp_path / "NSEScripMaster.txt"
    master_file.write_bytes(kit.security_master_bytes(
        [kit.master_row(1000 + n, f"S{n}", "EQ", f"Company {n}", "0.01", "10", stock_isin(n), f"SYM{n}")
         for n in range(1, 501)]
    ))
    common = ["--root", str(root), "--workspace", "india"]
    code, out = run(capsys, "ingest-master", *common, "--file", str(master_file), "--snapshot-date", "2026-10-01")
    assert code == 0 and out["result"]["row_count"] == 500 and out["caveats"][0]["code"] == "SURVIVORSHIP_BIAS"
    with PilotDataStore(root, workspace="india") as store:
        assert store.query("SELECT locator, source FROM source_files") == [("NSEScripMaster.txt", "local_file")]
    members = [{"Company Name": f"Company {n}", "Industry": "I", "Symbol": f"SYM{n}", "Series": "EQ",
                "ISIN Code": stock_isin(n)} for n in range(1, 501)]
    small = [{"Company Name": f"Small {n}", "Industry": "I", "Symbol": f"SM{n}", "Series": "EQ",
              "ISIN Code": stock_isin(2000 + n)} for n in range(1, 251)]
    client = mock_client({
        NIFTY500_URL: kit.index_list_csv(members + [kit.DUMMY_INDEX_ROW]),
        SMALLCAP250_URL: kit.index_list_csv(small + [kit.DUMMY_INDEX_ROW]),
        ASM_URL: kit.asm_json([kit.asm_row("S1", stock_isin(1))], []),
        GSM_URL: kit.gsm_json([kit.gsm_row("S1", stock_isin(1))]),
    })
    code, out = run(capsys, "fetch-lists", *common, "--min-interval-seconds", "0", client=client)
    assert code == 0 and [item["list_name"] for item in out["result"]["lists"]] == ["nifty500", "smallcap250"]
    code, out = run(capsys, "fetch-surveillance", *common, "--min-interval-seconds", "0", client=client)
    assert code == 0 and out["result"]["asm"]["entry_count"] == 1 and out["result"]["gsm"]["entry_count"] == 1
    # the master (2026-10-01) is later than the 60 bhavcopy days: give it history first
    with PilotDataStore(root, workspace="india") as store:
        for day in trading_dates(60, end=date(2026, 9, 30)):
            ingest_udiff(store, desc("udiff_cm", "nse_archive", f"https://nsearchives.nseindia.com/u/{day}"),
                         kit.udiff_zip(day, [kit.udiff_row("SYM9", "EQ", stock_isin(509), "10", "11", "9", "10",
                                                           trade_date=day)]), trade_date=day)
    code, out = run(capsys, "build-targets", *common, "--as-of", "2026-10-01")
    assert code == 0 and out["result"]["members"] == 500 and out["result"]["exclusions"] == 0
    assert len(out["result"]["target_sha256"]) == 64


def test_cli_errors_exit_two_with_the_code_and_caveats_first(tmp_path, capsys):
    code, out = run(capsys, "build-targets", "--root", str(tmp_path / "r"), "--workspace", "india",
                    "--as-of", "2026-10-01")
    assert code == 2 and out["error_code"] == "security_master_missing"
    assert out["caveats"][0]["code"] == "SURVIVORSHIP_BIAS"
    code, out = run(capsys, "fetch-lists", "--root", str(tmp_path / "r"), "--workspace", "india",
                    "--min-interval-seconds", "0", client=mock_client({}))
    assert code == 2 and out["error_code"] == "index_list_unavailable"
