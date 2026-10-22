"""G-3: the operator-pinned NSE calendar and the band for a session whose files do not exist yet (64-08 Task 1)."""

import ast
import hashlib
import json
import shutil
import socket
from datetime import date, timedelta
from pathlib import Path

import pytest

from pilot_data import nse_calendar, price_bands, sessions
from pilot_data.bhavcopy import ingest_udiff
from pilot_data.core import PilotDataError
from pilot_data.nse_calendar import UpcomingSessionCheck, load_pinned_calendar, upcoming_session_check
from pilot_data.nse_http import NseHttp
from pilot_data.price_bands import (
    BandConvention,
    BandResolver,
    band_agreement,
    band_for_upcoming_session,
    band_on,
    record_band_convention,
)
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit
import test_pilot_data_price_bands as pb

S1, S2, S3, S4, S5 = pb.SESS  # Mon 3 March to Fri 7 March 2025
SAT, SUN = S5 + timedelta(days=1), S5 + timedelta(days=2)
MON = S5 + timedelta(days=3)
INSTRUMENTS = [("RELIANCE", "EQ", pb.RELIANCE), ("CANBK", "EQ", pb.CANBK), ("AGSTRA", "BZ", pb.AGSTRA)]
COVERAGE = (date(2025, 3, 1), date(2025, 3, 31))
ISINS = (pb.RELIANCE, pb.CANBK, pb.AGSTRA)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used in a G-3 test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(NseHttp, "__init__", refuse)


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


# --------------------------------------------------------------------------- builders
def calendar_doc(*, holidays=(), special=(), coverage=COVERAGE, basis="test_fixture"):
    return {
        "schema": "growin-nse-calendar/1", "exchange": "NSE", "segment": "CM", "basis": basis,
        "coverage": {"from": coverage[0].isoformat(), "to": coverage[1].isoformat()},
        "source": {"publisher": "test fixture", "reference": "none", "published_on": "2025-01-01",
                   "document_sha256": "d" * 64, "saved_by": "test", "saved_at_utc": "2025-01-02T00:00:00+00:00"},
        "holidays": [{"date": day.isoformat(), "name": "fixture holiday"} for day in holidays],
        "special_sessions": [{"date": day.isoformat(), "kind": "fixture special"} for day in special],
    }


def write_calendar(tmp_path, name="calendar.json", **kwargs):
    raw = json.dumps(calendar_doc(**kwargs), sort_keys=True).encode()
    path = tmp_path / name
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def pinned(tmp_path, **kwargs):
    path, digest = write_calendar(tmp_path, **kwargs)
    return load_pinned_calendar(path, expected_sha256=digest)


def day_bars(store, day, *, instruments=INSTRUMENTS, close="100", symbols=None):
    rows = [kit.udiff_row((symbols or {}).get(symbol, symbol), series, isin, close, "101", "99", close, trade_date=day)
            for symbol, series, isin in instruments]
    ingest_udiff(store, pb.desc("udiff_cm"), kit.udiff_zip(day, rows), trade_date=day)


def prior_world(store, *, lists=None):
    """S1 and S2 are sessions with bars; S3 (the forecast session) has nothing yet. Lists filed F0, S1, S2."""
    day_bars(store, S1)
    day_bars(store, S2)
    pb.mark_calendar(store, date(2025, 3, 1), S2, {S1, S2})
    pb.with_convention(store)
    for filed in (pb.F0, S1, S2):
        pb.put_list(store, filed, rows=lists)


def forecast(store, calendar, isin=pb.RELIANCE, *, prior=S2, session=S3):
    return band_for_upcoming_session(store, isin=isin, prior_session=prior, session=session, calendar=calendar,
                                     workspace="india")


def dump(observations):
    return [obs.model_dump_json() for obs in observations]


def forecasts(store, calendar):
    return dump(forecast(store, calendar, isin) for isin in (*ISINS, "INE040A01034"))


# --------------------------------------------------------------------------- the loader
def test_the_loader_returns_unpinned_for_no_path_no_pin_or_no_file(tmp_path):
    path, digest = write_calendar(tmp_path)
    assert load_pinned_calendar(None, expected_sha256=digest) is None
    assert load_pinned_calendar(path, expected_sha256=None) is None
    assert load_pinned_calendar(tmp_path / "absent.json", expected_sha256=digest) is None
    assert load_pinned_calendar(path, expected_sha256=digest).sha256 == digest


def test_a_tampered_or_malformed_file_never_degrades_to_unpinned(tmp_path):
    path, digest = write_calendar(tmp_path)
    path.write_bytes(path.read_bytes() + b" ")  # one byte
    with pytest.raises(PilotDataError) as caught:
        load_pinned_calendar(path, expected_sha256=digest)
    assert caught.value.code == "calendar_pin_mismatch"
    for name, mutate in {
        "unknown_key": lambda d: d | {"extra": 1},
        "wrong_schema": lambda d: d | {"schema": "growin-nse-calendar/2"},
        "weekend_holiday": lambda d: d | {"holidays": [{"date": "2025-03-08", "name": "x"}]},
        "outside_coverage": lambda d: d | {"special_sessions": [{"date": "2026-01-01", "kind": "x"}]},
        "own_sha": lambda d: d | {"sha256": "0" * 64},
    }.items():
        raw = json.dumps(mutate(calendar_doc())).encode()
        bad = tmp_path / f"{name}.json"
        bad.write_bytes(raw)
        with pytest.raises(PilotDataError) as invalid:
            load_pinned_calendar(bad, expected_sha256=hashlib.sha256(raw).hexdigest())
        assert invalid.value.code == "calendar_invalid", name


# --------------------------------------------------------------------------- row 2: missing or unpinned
def test_a_missing_or_unpinned_calendar_makes_every_upcoming_session_unknown(store, tmp_path):
    prior_world(store)
    path, digest = write_calendar(tmp_path)
    unpinned = [
        load_pinned_calendar(None, expected_sha256=digest),
        load_pinned_calendar(tmp_path / "absent.json", expected_sha256=digest),
        load_pinned_calendar(path, expected_sha256=None),
    ]
    for calendar in unpinned:
        got = forecast(store, calendar)
        assert (got.status, got.reason, got.session) == ("unknown", "session_calendar_unknown", S3)
    outside = pinned(tmp_path, name="short.json", coverage=(date(2025, 3, 1), S2))
    assert forecast(store, outside).reason == "session_calendar_unknown"
    assert forecast(store, pinned(tmp_path)).status == "no_band"


# --------------------------------------------------------------------------- row 6: calendar edges
def check(store, tmp_path, *, prior, session, **calendar):
    return upcoming_session_check(store, prior_session=prior, session=session, calendar=pinned(tmp_path, **calendar))


def test_the_calendar_edges_each_give_their_own_single_reason(store, tmp_path):
    prior_world(store)
    pb.mark_calendar(store, S3, S5, {S3, S4, S5})
    ok = check(store, tmp_path, prior=S2, session=S3)
    assert isinstance(ok, UpcomingSessionCheck)
    assert (ok.status, ok.reason, ok.calendar_basis) == ("expected", None, "test_fixture") and len(ok.calendar_sha256) == 64
    cases = {
        "listed holiday": (dict(prior=S2, session=S3, holidays=[S3]), "session_not_expected"),
        "weekend": (dict(prior=S5, session=SAT), "session_not_expected"),
        "weekday special": (dict(prior=S2, session=S3, special=[S3]), "session_special_unsupported"),
        "saturday special": (dict(prior=S5, session=SAT, special=[SAT]), "session_special_unsupported"),
        "missing weekday between": (dict(prior=S1, session=S3), "prior_session_not_last"),
        "special between": (dict(prior=S5, session=MON, special=[SUN]), "prior_session_not_last"),
        "after the weekend": (dict(prior=S5, session=MON), None),
        "after a listed holiday": (dict(prior=S2, session=S4, holidays=[S3]), None),
        "session outside coverage": (dict(prior=S2, session=S3, coverage=(date(2025, 3, 1), S2)), "session_calendar_unknown"),
        "gap outside coverage": (dict(prior=S1, session=S3, coverage=(S3, date(2025, 3, 31))), "session_calendar_unknown"),
    }
    for name, (args, reason) in cases.items():
        got = check(store, tmp_path, **args)
        assert got.reason == reason, name
        assert got.status == ("unknown" if reason else "expected"), name
    with pytest.raises(PilotDataError):
        check(store, tmp_path, prior=S3, session=S3)


def test_a_prior_session_the_store_does_not_call_a_session_is_refused(store, tmp_path):
    prior_world(store)
    pb.mark_calendar(store, S3, S3, set())  # the store says S3 is a holiday
    assert check(store, tmp_path, prior=S3, session=S4).reason == "prior_session_not_session"
    assert check(store, tmp_path, prior=S4, session=S5).reason == "prior_session_not_session"  # nothing known


def test_store_evidence_that_contradicts_the_table_is_a_conflict(store, tmp_path):
    prior_world(store)
    assert check(store, tmp_path, prior=S2, session=S3, holidays=[S1]).reason == "session_calendar_conflict"
    pb.mark_calendar(store, S3, S3, set())  # a plain weekday the store calls a holiday
    assert check(store, tmp_path, prior=S3, session=S4).reason == "prior_session_not_session"
    pb.mark_calendar(store, S4, S4, {S4})
    assert check(store, tmp_path, prior=S4, session=S5).reason == "session_calendar_conflict"
    assert check(store, tmp_path, prior=S4, session=S5, holidays=[S3]).reason is None  # the table now agrees


# --------------------------------------------------------------------------- row 1: effective after S, and what is asked
def test_a_list_dated_s_is_effective_after_s_and_never_the_basis(store, tmp_path):
    prior_world(store)
    calendar = pinned(tmp_path)
    clean = forecasts(store, calendar)
    planted_rows = [dict(kit.SEC_LIST_RELIANCE, Band="10"), dict(kit.SEC_LIST_CANBK, Band="5"),
                    dict(kit.SEC_LIST_AGSTRA, Band="20")]
    planted = pb.put_list(store, S3, rows=planted_rows)
    assert forecasts(store, calendar) == clean
    assert all(planted.source_sha256 not in json.loads(item)["source_sha256s"] for item in clean)
    # rewriting its rows (a different file dated S) changes nothing either
    pb.put_list(store, S3, rows=[dict(row, Band="2") for row in planted_rows])
    assert forecasts(store, calendar) == clean
    assert [json.loads(item)["percent"] for item in clean[:3]] == [None, None, "2.00"]


def test_the_store_is_never_asked_about_a_day_after_the_prior_session(store, tmp_path, monkeypatch):
    prior_world(store)
    pb.mark_calendar(store, S3, S5, {S3, S4, S5})  # evidence after prior exists and must stay unread
    asked: list[date] = []
    real_status, real_latest = sessions.day_status, sessions._latest_attempts

    def spy_status(handle, day):
        asked.append(day)
        return real_status(handle, day)

    def spy_latest(handle, start, end):
        asked.extend((start, end))
        return real_latest(handle, start, end)

    for module in (sessions, price_bands, nse_calendar):
        if hasattr(module, "day_status"):
            monkeypatch.setattr(module, "day_status", spy_status)
    monkeypatch.setattr(sessions, "_latest_attempts", spy_latest)
    calendar = pinned(tmp_path)
    for isin in ISINS:
        assert forecast(store, calendar, isin).status in ("no_band", "fixed")
    assert asked and max(asked) <= S2


# --------------------------------------------------------------------------- row 3: symbol and series moves
def seeded_with_s_list(root, rows):
    st = PilotDataStore(root, workspace="india")
    day_bars(st, S1)
    day_bars(st, S2)
    pb.mark_calendar(st, date(2025, 3, 1), S2, {S1, S2})
    pb.with_convention(st)
    pb.put_list(st, pb.F0)
    pb.put_list(st, S1)
    pb.put_list(st, S2, rows=rows)  # effective S3
    return st


def test_a_symbol_change_or_series_move_effective_on_s_is_not_carried(tmp_path):
    carried = [dict(kit.SEC_LIST_CANBK), dict(kit.SEC_LIST_AGSTRA)]
    calendar = pinned(tmp_path)
    renamed = [dict(kit.SEC_LIST_RELIANCE, Symbol="NEWSYM"), *carried]
    with seeded_with_s_list(tmp_path / "renamed", renamed) as st:
        got = forecast(st, calendar)
        assert (got.status, got.reason, got.nse_symbol, got.series) == (
            "unknown", "band_symbol_not_carried", "RELIANCE", "EQ")
        assert forecast(st, calendar, pb.CANBK).status == "no_band"
    with seeded_with_s_list(tmp_path / "moved", [dict(kit.SEC_LIST_RELIANCE, Series="BE"), *carried]) as st:
        got = forecast(st, calendar)
        assert (got.reason, got.series) == ("band_symbol_not_carried", "EQ")


def test_a_chain_only_basis_needs_a_list(store, tmp_path):
    day_bars(store, S1)
    day_bars(store, S2)
    pb.mark_calendar(store, date(2025, 3, 1), S2, {S1, S2})
    pb.with_convention(store)
    pb.put_list(store, pb.F0)
    pb.put_list(store, S1)  # effective S2; nothing effective on S3
    pb.put_changes(store, S2, [])  # effective S3
    got = forecast(store, pinned(tmp_path))
    assert (got.status, got.reason) == ("unknown", "upcoming_band_needs_list")


def test_an_isin_with_no_prior_bar_or_two_pairs_is_unknown(store, tmp_path):
    prior_world(store)
    calendar = pinned(tmp_path)
    assert forecast(store, calendar, "INE040A01034").reason == "no_bar_on_prior_session"
    with PilotDataStore(tmp_path / "two", workspace="india") as two:
        day_bars(two, S1)
        pb.mark_calendar(two, date(2025, 3, 1), S1, {S1})
        rows = [kit.udiff_row("DUALA", "EQ", pb.CANBK, "1", "1", "1", "1", trade_date=S1, token="1"),
                kit.udiff_row("DUALB", "EQ", pb.CANBK, "1", "1", "1", "1", trade_date=S1, token="2")]
        ingest_udiff(two, pb.desc("udiff_cm"), kit.udiff_zip(S1, rows), trade_date=S1)
        pb.with_convention(two)
        pb.put_list(two, S1)
        got = forecast(two, calendar, pb.CANBK, prior=S1, session=S2)
        assert got.reason == "band_isin_ambiguous"


# --------------------------------------------------------------------------- row 4: agreement after S lands
def land_session(store, day, *, symbols=None, close="100", instruments=INSTRUMENTS):
    day_bars(store, day, symbols=symbols, close=close, instruments=instruments)
    pb.mark_calendar(store, day, day, {day})


def test_after_s_lands_band_on_and_the_forecast_agree(store, tmp_path):
    prior_world(store, lists=None)
    # one ISIN whose pair the S list no longer carries (the list-basis case that has no band to read)
    calendar = pinned(tmp_path)
    before = forecasts(store, calendar)
    land_session(store, S3)
    after = forecasts(store, calendar)
    assert after == before
    for isin in ISINS:
        agreement = band_agreement(forecast(store, calendar, isin), band_on(store, isin=isin, session=S3, workspace="india"))
        assert agreement.agree and agreement.differing == (), isin
    assert [json.loads(item)["status"] for item in after[:3]] == ["no_band", "no_band", "fixed"]


def test_agreement_covers_the_not_carried_case_and_the_row_conflict(store, tmp_path):
    carried = [dict(kit.SEC_LIST_RELIANCE, Series="BE"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA]
    day_bars(store, S1)
    day_bars(store, S2)
    pb.mark_calendar(store, date(2025, 3, 1), S2, {S1, S2})
    pb.with_convention(store)
    pb.put_list(store, pb.F0)
    pb.put_list(store, S1)
    pb.put_list(store, S2, rows=carried)
    calendar = pinned(tmp_path)
    forecast_unknown = forecast(store, calendar)
    land_session(store, S3)
    actual = band_on(store, isin=pb.RELIANCE, session=S3, workspace="india")
    assert (forecast_unknown.reason, actual.reason) == ("band_symbol_not_carried", "not_in_band_list")
    assert band_agreement(forecast_unknown, actual).agree  # reasons are not compared; every compared field is equal


def test_a_calendar_that_calls_a_real_holiday_a_session_is_disagreement(store, tmp_path):
    prior_world(store)
    wrong = pinned(tmp_path)  # says S3 trades
    predicted = forecast(store, wrong)
    pb.mark_calendar(store, S3, S3, set())  # the store settles S3 as a holiday
    actual = band_on(store, isin=pb.RELIANCE, session=S3, workspace="india")
    verdict = band_agreement(predicted, actual)
    assert not verdict.agree
    assert {"status", "nse_symbol", "series", "source_kind", "source_sha256s"} <= set(verdict.differing)
    # hashes compare as a set: order alone never disagrees
    same = predicted.model_copy(update={"source_sha256s": tuple(reversed(("a" * 64, "b" * 64)))})
    other = predicted.model_copy(update={"source_sha256s": ("a" * 64, "b" * 64)})
    assert band_agreement(same, other).agree


# --------------------------------------------------------------------------- row 5: look-ahead
def test_nothing_dated_s_or_later_moves_the_forecast(store, tmp_path):
    prior_world(store)
    calendar = pinned(tmp_path)
    clean = forecasts(store, calendar)
    day_bars(store, S3, symbols={"RELIANCE": "NEWSYM", "CANBK": "NEWCAN"}, close="160")
    day_bars(store, S4, close="170")
    pb.mark_calendar(store, S3, S5, {S3, S4})
    pb.put_list(store, S3, rows=[dict(kit.SEC_LIST_RELIANCE, Symbol="NEWSYM", Band="5")])
    pb.put_changes(store, S3, [pb.change("RELIANCE", "No Band", "20")])
    assert forecasts(store, calendar) == clean


# --------------------------------------------------------------------------- row 7: no writes (the RLC fixture)
def rlc_world(root: Path, changes_rule: str) -> None:
    """S-2=S1, S-1=S2, S=S3. L0 filed S1 (effective S2), L1 filed S2 (effective S3), one changes file C effective S3.

    From L0 to L1 three rows move and C explains them; RELIANCE moves No Band to 10 while C says 20.
    """
    with PilotDataStore(root, workspace="india") as st:
        day_bars(st, S1)
        day_bars(st, S2)
        pb.mark_calendar(st, date(2025, 3, 1), S2, {S1, S2})
        record_band_convention(st, BandConvention(list_rule="next_session_after_file_date", changes_rule=changes_rule,
                                                  basis="test_fixture", evidence_sha256="e" * 64,
                                                  recorded_at_utc=pb.LATER), workspace="india")
        movers = ["XA0", "XA1", "XA2"]
        before = [*pb.STANDARD, *(pb.extra(s, "20") for s in movers)]
        after = [dict(kit.SEC_LIST_RELIANCE, Band="10"), kit.SEC_LIST_CANBK, kit.SEC_LIST_AGSTRA,
                 *(pb.extra(s, "10") for s in movers)]
        pb.put_list(st, S1, rows=before)
        pb.put_list(st, S2, rows=after)
        filed = S2 if changes_rule == "next_session_after_file_date" else S3
        pb.put_changes(st, filed, [pb.change("RELIANCE", "No Band", "20"),
                                   *(pb.change(s, "20", "10") for s in movers)])


def quarantine_state(st):
    rows = st.query("SELECT quarantine_id FROM quarantine_records ORDER BY quarantine_id")
    return len(rows), [row[0] for row in rows]


def file_sha(root: Path) -> str:
    return hashlib.sha256((root / "pilot_india.duckdb").read_bytes()).hexdigest()


@pytest.mark.parametrize("changes_rule", ["next_session_after_file_date", "file_date"])
def test_a_forecast_writes_no_quarantine_even_when_the_conflict_is_reachable(tmp_path, changes_rule):
    root = tmp_path / "rlc"
    rlc_world(root, changes_rule)
    calendar = pinned(tmp_path)
    # precondition: on a throwaway copy that holds S's own evidence, the same files do write the quarantine
    copy_root = tmp_path / "copy"
    shutil.copytree(root, copy_root)
    with PilotDataStore(copy_root, workspace="india") as copy:
        land_session(copy, S3)
        resolver = BandResolver(copy)
        basis = resolver.basis(S3)
        assert basis.kind == "list" and set(basis.conflicts) == {("RELIANCE", "EQ")}
        written = copy.query("SELECT count(*) FROM quarantine_records WHERE reason_code = ? AND date_from = ?",
                             ["band_crosscheck_row_conflict", S3])[0][0]
        assert written > 0
    # the forecast store carries no S evidence and gets no write
    with PilotDataStore(root, workspace="india") as st:
        count, ids = quarantine_state(st)
        assert count == 0
        got = forecast(st, calendar)
        assert (got.status, got.reason) == ("unknown", "band_crosscheck_row_conflict")
        assert (got.nse_symbol, got.series) == ("RELIANCE", "EQ") and len(got.source_sha256s) == 3
        assert forecast(st, calendar, pb.CANBK).status == "no_band"
        assert forecast(st, calendar, pb.AGSTRA).percent is not None
        assert quarantine_state(st) == (count, ids)
    digest = file_sha(root)
    with PilotDataStore(root, workspace="india") as st:
        for isin in ISINS:
            forecast(st, calendar, isin)
    assert file_sha(root) == digest
    with PilotDataStore(root, workspace="india", read_only=True) as read_only:
        assert forecast(read_only, calendar).reason == "band_crosscheck_row_conflict"
    assert file_sha(root) == digest


# --------------------------------------------------------------------------- row 9: no network
def test_nse_calendar_imports_no_http_or_relay_module():
    tree = ast.parse(Path(nse_calendar.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)
    banned = [name for name in imported if "httpx" in name or "nse_http" in name or "relay" in name or "socket" in name]
    assert banned == []


def test_the_forecast_makes_no_network_call(store, tmp_path):
    prior_world(store)
    assert forecast(store, pinned(tmp_path)).status == "no_band"  # the autouse ban raises on any connect or NseHttp
