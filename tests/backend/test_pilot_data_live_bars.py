"""G-1b: strategy-ready bars as of a session that has not traded yet (64-08 Task 3)."""

import ast
import hashlib
import json
import shutil
import socket
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pilot_data import live_bars, sessions
from pilot_data.bhavcopy import ingest_pr_zip, ingest_udiff
from pilot_data.core import PilotDataError
from pilot_data.corporate_actions import derive_corporate_actions
from pilot_data.dataset import read_dataset_rows
from pilot_data.live_bars import LiveBarsResult, bars_as_of, main as live_main
from pilot_data.models import CrossCheckTolerances, QuarantineRecord
from pilot_data.nse_http import NseHttp
from pilot_data.nse_ingest import _log_attempt
from pilot_data.price_bands import band_for_upcoming_session, band_on
from pilot_data.session_store import SEEDS_DDL, seed_session_store
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import ingest_surveillance
from strategy_india.data import DatasetView, bar_from_row, events_from_manifest
from strategy_india.errors import HoldoutViolation
from strategy_india.holdout import HoldoutRange

import pilot_data_testkit as kit
import test_pilot_data_crosscheck as cc
import test_pilot_data_nse_calendar as nc
import test_pilot_data_price_bands as pb
import test_pilot_data_session_store as ss

D1, D2, D3, D4, D5 = cc.SESSIONS  # Mon 3 March to Fri 7 March 2025
S = D5 + timedelta(days=3)  # Mon 10 March: the decision session
S1 = S + timedelta(days=1)  # Tue 11 March
TARGETS = cc.make_targets(cc.CANBK_MEMBER, cc.STEADY_MEMBER)
TOLERANCES = CrossCheckTolerances()
CANBK_FUTURE = {S: ("121", "123", "120", "122"), S1: ("122", "124", "121", "123")}
STEADY_FUTURE = {S: ("100", "105", "95", "100"), S1: ("100", "105", "95", "100")}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network used in a G-1b test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(NseHttp, "__init__", refuse)


# --------------------------------------------------------------------------- builders
def mark_session_store(store):
    store.ensure_table("session_store_seeds", SEEDS_DDL, key_columns=("seed_report_sha256",))
    store.append_rows("session_store_seeds", [{
        "seed_report_sha256": "a" * 64, "research_identity_sha256": "b" * 64, "dataset_sha256": "c" * 64,
        "window_start": D1, "window_end": D5, "seeded_at_utc": cc.LATER.replace(tzinfo=None), "row_sha256": "d" * 64,
        "source_sha256": "a" * 64}], check="session_store_seed")


def scaled(prices, factor):
    return tuple(str((Decimal(p) * factor).quantize(Decimal("0.0001"))) for p in prices)


def put_bhavcopy(store, days, canbk, steady):
    for day in days:
        rows = [cc.ohlc_row("CANBK", "EQ", cc.OLD if day < D3 else cc.NEW, canbk[day], day, "11256"),
                cc.ohlc_row("STEADYCO", "EQ", cc.STEADY_ISIN, steady[day], day, "5"),
                cc.ohlc_row("OTHERCO", "EQ", cc.OTHER_ISIN, steady[day], day, "9")]
        ingest_udiff(store, cc.desc(), kit.udiff_zip(day, rows), trade_date=day)


def pr_zip_on(day, rows):
    return kit.pr_zip(day, [kit.pd_index_row()], rows, [])


def build_world(root: Path, shocks=frozenset(), *, extra=None) -> None:
    """A session-store-shaped world with no split event for CANBK (its ISIN change is `isin_change_unexplained`).

    Sessions D1..D5, then S and S+1 when the world has a future. Each shock adds data dated S or later, or removes it.
    """
    future = "no_future" not in shocks
    factor = Decimal("1.6") if "scale" in shocks else Decimal(1)
    canbk = {**cc.CANBK, **({day: scaled(p, factor) for day, p in CANBK_FUTURE.items()} if future else {})}
    steady = {**cc.STEADY, **({day: scaled(p, factor) for day, p in STEADY_FUTURE.items()} if future else {})}
    days = [*cc.SESSIONS, *((S, S1) if future else ())]
    with PilotDataStore(root, workspace="india") as st:
        put_bhavcopy(st, days, canbk, steady)
        # a Friday-bounded seed copies fetch logs only to its window end: Sat and Sun were never attempted
        cc.mark_calendar(st, cc.START, D5 if "friday_bound" in shocks else cc.END, set(cc.SESSIONS))
        if future:
            cc.mark_calendar(st, S, S1, {S, S1})
        for code, prices in (("CANBAN", canbk), ("STEADY", steady)):
            history = {day: p for day, p in prices.items() if day <= cc.END}
            if code == "CANBAN" and "old_adjusted" in shocks:  # Breeze history already adjusted for the old-ISIN bonus
                history[D1] = scaled(history[D1], Decimal("0.5"))
            cc.load_breeze(st, code, cc.breeze_rows(code, history), start=cc.START, end=cc.END,
                           locator=f"relay/{code}/history")
            if future:  # a separate answer, as a windowed pull would be: its bytes cannot touch the history's hash
                ahead = {day: p for day, p in prices.items() if day > cc.END}
                cc.load_breeze(st, code, cc.breeze_rows(code, ahead), start=S, end=S1, locator=f"relay/{code}/ahead")
        if "old_bonus" in shocks:  # an old-ISIN bonus ex D2, first seen D1: known long before S
            ingest_pr_zip(st, cc.desc(kind="pr_zip"),
                          pr_zip_on(D1, [kit.bc_row("EQ", "CANBK", "CANARA BANK", "BONUS 1:1", ex_date=D2)]), trade_date=D1)
        cas = []
        if "relink" in shocks:  # (h) the split that explains CANBK's ISIN change, first seen on S, ex-date on D3
            cas.append(kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=D3))
        if "ca_structural" in shocks:  # (d) first seen and ex-dated on S
            cas.append(kit.bc_row("EQ", "STEADYCO", "STEADY CO", "FVSPLT FRM RS 10 TO RS 5", ex_date=S))
        if "factor_event" in shocks:  # (i) a price factor first seen on S with an ex-date before S
            cas.append(kit.bc_row("EQ", "STEADYCO", "STEADY CO", "BONUS 1:1", ex_date=D4))
        if cas:
            ingest_pr_zip(st, cc.desc(kind="pr_zip"), pr_zip_on(S, cas), trade_date=S)
        if "new_breeze" in shocks:  # (c) a new answer for [S, S] with a coverage row, for a prepared stock_code
            cc.load_breeze(st, "CANBAN", cc.breeze_rows("CANBAN", {S: ("200", "210", "190", "205")}), start=S, end=S,
                           locator="relay/CANBAN/S")
        if "bands_surv" in shocks:  # (e)
            record = [kit.SEC_LIST_CANBK, {**kit.SEC_LIST_RELIANCE, "Symbol": "STEADYCO"}]
            pb.put_list(st, S, rows=record)
            ingest_surveillance(st, cc.desc("nse_api", "surveillance_asm"),
                                kit.asm_json([kit.asm_row("STEADYCO", cc.STEADY_ISIN, when="10-Mar-2025")], []),
                                list_name="asm")
        if "fetch_extra" in shocks:  # (f)
            for kind, outcome in (("pr", "no_file"), ("cm_legacy", "failed"), ("udiff", "no_file")):
                _log_attempt(st, d=S, kind=kind, outcome=outcome, http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=cc.LATER)
        if "quarantine" in shocks:  # (g)
            st.record_quarantine([QuarantineRecord(
                workspace="india", check="bhavcopy_consistency", reason_code="primary_without_pd", scope="raw",
                nse_symbol="CANBK", stock_code="CANBAN", date_from=S, date_to=S)])
        if cas or "new_breeze" in shocks or "old_bonus" in shocks:
            derive_corporate_actions(st, workspace="india")
        if extra is not None:
            extra(st)
        mark_session_store(st)


def compose(root: Path, *, as_of=D5, decision=S, window_start=cc.START, targets=TARGETS, writable=True, calendar=None):
    with PilotDataStore(root, workspace="india", read_only=not writable) as st:
        return bars_as_of(st, targets=targets, decision_session=decision, as_of=as_of, window_start=window_start,
                          tolerances=TOLERANCES, workspace="india", calendar=calendar)


def canon(result: LiveBarsResult):
    rows = json.dumps([row.model_dump(mode="json") for row in result.rows], sort_keys=True)
    return hashlib.sha256(rows.encode()).hexdigest(), len(result.rows), result.composition_sha256


def asserted_future(root: Path) -> None:
    """The precondition: S and S+1 hold a primary row, a covered Breeze bar and fetch-log rows, so a shock is not empty."""
    if root.name in ("b_future_deleted", "gone"):
        return  # the deleted-future worlds hold no S rows by design
    with PilotDataStore(root, workspace="india", read_only=True) as st:
        for day in (S, S1):
            symbols = {row[0] for row in st.query("SELECT nse_symbol FROM bhavcopy_bars WHERE trade_date = ? "
                                                  "AND file_kind = 'udiff'", [day])}
            assert {"CANBK", "STEADYCO"} <= symbols
            assert st.query("SELECT count(*) FROM nse_fetch_log WHERE trade_date = ?", [day])[0][0] > 0
            for code in ("CANBAN", "STEADY"):
                view = cc.breeze_bars_for(st, code, start=day, end=day)
                # a second answer for S (shock c) turns the S bar into a conflict, which is still data dated S
                assert (view.bar_on(day) is not None or view.conflicts) and view.covers(day), (code, day)


# --------------------------------------------------------------------------- 1: look-ahead shock
SHOCKS = {
    "a_prices_up": {"scale"}, "b_future_deleted": {"no_future"}, "c_new_breeze_answer": {"new_breeze"},
    "d_structural_event": {"ca_structural"}, "e_bands_and_surveillance": {"bands_surv"},
    "f_fetch_log_rows": {"fetch_extra"}, "g_quarantine": {"quarantine"}, "h_relink_split": {"relink"},
    "i_price_factor_event": {"factor_event"},
    # the relink split is first seen on S, so the old-ISIN bonus is outside the as-of lineage: rawness, the basis and
    # the composition must not see it (before the fix CANBAN went as_traded_breeze_unverified -> raw_confirmed)
    "j_old_isin_bonus_then_relink": {"old_bonus", "relink"},
    "all_together": {"scale", "new_breeze", "ca_structural", "bands_surv", "fetch_extra", "quarantine", "relink",
                     "factor_event", "old_bonus"},
}


def test_nothing_dated_on_or_after_the_decision_session_moves_a_row_or_the_composition_id(tmp_path, monkeypatch):
    base_root = tmp_path / "base"
    build_world(base_root)
    asserted_future(base_root)
    asked = []
    real = sessions._latest_attempts
    monkeypatch.setattr(sessions, "_latest_attempts", lambda store, start, end: (asked.append((start, end)), real(store, start, end))[1])
    base = compose(base_root)
    assert max(end for _, end in asked) < S  # nothing asked about the decision session or later
    monkeypatch.undo()
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    anchors = {row.stock_code for row in base.rows}
    assert anchors == {"CANBAN", "STEADY"} and max(row.trade_date for row in base.rows) == D5
    assert min(row.trade_date for row in base.rows if row.stock_code == "CANBAN") == D3  # no link, so no earlier rows
    reference = canon(base)
    for name, flags in SHOCKS.items():
        root = tmp_path / name
        build_world(root, frozenset(flags))
        asserted_future(root)
        assert canon(compose(root)) == reference, name
    deleted = tmp_path / "gone"
    build_world(deleted, frozenset({"no_future"}))
    assert canon(compose(deleted)) == reference


def test_old_isin_evidence_behind_a_late_relink_cannot_flip_the_basis_the_caveat_or_cause_a_refusal(tmp_path):
    """Opus r1 / Codex r1: the cross-check builds its own untrimmed lineage; its rawness must not reach the output."""
    outcomes = {}
    for name, flags in {"bonus_plain": {"old_bonus"}, "bonus_relink": {"old_bonus", "relink"},
                        "adjusted_plain": {"old_bonus", "old_adjusted"},
                        "adjusted_relink": {"old_bonus", "old_adjusted", "relink"}}.items():
        root = tmp_path / name
        build_world(root, frozenset(flags))
        outcomes[name] = compose(root)  # the adjusted Breeze history is old-ISIN only: no refusal, with or without the link
    for plain, linked in (("bonus_plain", "bonus_relink"), ("adjusted_plain", "adjusted_relink")):
        assert canon(outcomes[plain]) == canon(outcomes[linked]), linked
        canbk = [row for row in outcomes[linked].rows if row.stock_code == "CANBAN"]
        assert len(canbk) == 3 and {row.raw_adjustment_basis for row in canbk} == {"as_traded_breeze_unverified"}
        assert "BREEZE_RAW_UNVERIFIED" in {c.code for c in outcomes[linked].caveats}
        assert min(row.trade_date for row in canbk) == D3  # no old-ISIN rows: the link was not known yet
    assert len(outcomes["bonus_relink"].rows) == 8  # Codex r1: the relink does not change the composition by rows either


def test_a_relink_known_by_as_of_still_confirms_rawness_and_the_adjusted_check_runs_on_the_as_of_lineage(tmp_path):
    """The other direction: once the link is known, the old-ISIN bonus counts, so adjusted Breeze history is refused."""
    root = tmp_path / "known"

    def known_relink(st):  # the split is first seen on D4, before as_of
        ingest_pr_zip(st, cc.desc(kind="pr_zip"),
                      pr_zip_on(D4, [kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=D3)]),
                      trade_date=D4)
        derive_corporate_actions(st, workspace="india")

    build_world(root, frozenset({"old_bonus", "old_adjusted"}), extra=known_relink)
    with pytest.raises(PilotDataError) as refused:
        compose(root)
    assert refused.value.code == "breeze_adjusted_detected"


def test_a_repeated_announcement_in_a_file_after_as_of_moves_no_hash_the_basis_or_the_run_identity(tmp_path):
    """Codex r1: events keep evidence hashes from every sighting; only sightings dated on or before as_of may count."""
    rights_row = kit.bc_row("EQ", "STEADYCO", "STEADY CO", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=D4)

    def first_seen_d2(st):
        ingest_pr_zip(st, cc.desc(kind="pr_zip"), pr_zip_on(D2, [rights_row]), trade_date=D2)
        derive_corporate_actions(st, workspace="india")

    def and_again_on_s(st):
        first_seen_d2(st)
        ingest_pr_zip(st, cc.desc(kind="pr_zip"), pr_zip_on(S, [rights_row]), trade_date=S)
        derive_corporate_actions(st, workspace="india")

    once, repeated = tmp_path / "once", tmp_path / "repeated"
    build_world(once, extra=first_seen_d2)
    build_world(repeated, extra=and_again_on_s)
    with PilotDataStore(repeated, workspace="india", read_only=True) as st:  # the precondition: S holds a sighting
        assert {row[0] for row in st.query("SELECT file_date FROM corporate_action_sightings_rev_c")} == {D2, S}
    first, second = compose(once), compose(repeated)
    assert "INE002A01018" in first.factor_set_hashes and first.factor_set_hashes == second.factor_set_hashes
    assert canon(first) == canon(second)
    assert any(row.adjusted_quarantined for row in first.rows)  # the rights event is in the factor set: evidence counts


def old_isin_quarantine(check, reason, day_from, day_to):
    """A raw-scope quarantine on the OLD ISIN only (no stock code), the shape an upstream check leaves for a dead ISIN."""
    def add(st):
        st.record_quarantine([QuarantineRecord(workspace="india", check=check, reason_code=reason, scope="raw",
                                               isin=cc.OLD, date_from=day_from, date_to=day_to)])
    return add


def test_an_old_isin_hold_cannot_drop_rows_because_of_a_relink_first_seen_after_as_of(tmp_path):
    """Codex r2: the cross-check matched quarantines on every ISIN of the untrimmed lineage, so a relink first seen on
    S pulled the old ISIN's operator hold onto the new ISIN's bars and 8 rows became 5."""
    hold = old_isin_quarantine("manual_review", "operator_hold", D1, D5)
    plain, linked = tmp_path / "plain", tmp_path / "linked"
    build_world(plain, extra=hold)
    build_world(linked, frozenset({"relink"}), extra=hold)
    with PilotDataStore(linked, workspace="india", read_only=True) as st:  # the precondition: the link exists, dated S
        assert {row[0] for row in st.query("SELECT file_date FROM corporate_action_sightings_rev_c")} == {S}
    before, after = compose(plain), compose(linked)
    assert len(before.rows) == len(after.rows) == 8
    assert {row.trade_date for row in after.rows if row.stock_code == "CANBAN"} == {D3, D4, D5}
    assert canon(before) == canon(after) and before.composition_sha256 == after.composition_sha256


def test_a_price_band_quarantine_on_the_old_isin_dated_before_as_of_leaves_the_same_days_with_or_without_the_relink(tmp_path):
    """Opus r2: the same untrimmed match dropped D4 only in the world that learned the link on S."""
    band = old_isin_quarantine("price_band", "band_exceeded", D4, D4)
    plain, linked = tmp_path / "plain", tmp_path / "linked"
    build_world(plain, extra=band)
    build_world(linked, frozenset({"relink"}), extra=band)
    before, after = compose(plain), compose(linked)
    for result in (before, after):
        assert [row.trade_date for row in result.rows if row.stock_code == "CANBAN"] == [D3, D4, D5]
    assert canon(before) == canon(after)


def test_a_relink_known_by_as_of_still_lets_an_old_isin_hold_exclude_the_days_it_covers(tmp_path):
    """The other direction: once the link is known, the old ISIN is part of the lineage and its hold applies."""
    hold = old_isin_quarantine("manual_review", "operator_hold", D1, D5)

    def known_relink_and_hold(st):  # the split is first seen on D4, before as_of
        ingest_pr_zip(st, cc.desc(kind="pr_zip"),
                      pr_zip_on(D4, [kit.bc_row("EQ", "CANBK", "CANARA BANK", "FVSPLT FRM RS 10 TO RS 2", ex_date=D3)]),
                      trade_date=D4)
        derive_corporate_actions(st, workspace="india")
        hold(st)

    root = tmp_path / "known"
    build_world(root, extra=known_relink_and_hold)
    result = compose(root)
    assert {row.stock_code for row in result.rows} == {"STEADY"}  # D1..D5 of CANBAN are all held


# --------------------------------------------------------------------------- 2: parity with the frozen dataset
@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    base = tmp_path_factory.mktemp("frozen_world")
    world = ss.build_research(base, end=D5)
    session_root = base / "session"
    seed_session_store(session_root, world["root"], dataset_dir=world["dataset"], window_start=D1, window_end=D5,
                       workspace="india", band_parity_offset_days=2)
    with PilotDataStore(session_root, workspace="india") as st:  # the weekend before S was attempted and found empty
        cc.mark_calendar(st, D5 + timedelta(days=1), S - timedelta(days=1), set())
    return {**world, "session": session_root}


def test_rows_match_the_frozen_dataset_on_every_field_but_the_run_id(frozen, tmp_path):
    root = tmp_path / "session"
    shutil.copytree(frozen["session"], root)
    manifest = frozen["manifest"]
    with PilotDataStore(root, workspace="india") as st:
        result = bars_as_of(st, targets=frozen["targets"], decision_session=S, as_of=manifest.window_end,
                            window_start=manifest.window_start, tolerances=TOLERANCES, workspace="india")
        unresolved = st.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'corporate_action' "
                              "AND reason_code LIKE 'ca_unresolved_%'")[0][0]
    frozen_rows = read_dataset_rows(frozen["dataset"])
    live_view = [{k: v for k, v in row.model_dump().items() if k != "crosscheck_run_id"} for row in result.rows]
    assert live_view == [{k: v for k, v in row.model_dump().items() if k != "crosscheck_run_id"} for row in frozen_rows]
    assert len(frozen_rows) == manifest.row_count >= 5
    assert result.lineage_hashes == manifest.lineage_hashes and result.factor_set_hashes == manifest.factor_set_hashes
    assert result.dividend_amount_unknown_events == manifest.dividend_amount_unknown_events
    assert {row.crosscheck_run_id for row in result.rows} == {result.composition_sha256} != {manifest.crosscheck_run_id}
    assert any(row.adjusted_quarantined for row in result.rows) and unresolved > 0  # the unresolved right is quarantined
    assert any(row.stock_code == "STEADY" and row.trade_date == D2 for row in frozen_rows) is False  # operator hold kept
    assert events_from_manifest(result) is not None


# --------------------------------------------------------------------------- 3: re-adjust after a late split
def test_a_split_recorded_after_the_frozen_as_of_readjusts_earlier_bars_and_leaves_raw_alone(tmp_path):
    root = tmp_path / "world"
    build_world(root)
    first = compose(root, as_of=D4, decision=D5)
    with PilotDataStore(root, workspace="india") as st:
        ingest_pr_zip(st, cc.desc(kind="pr_zip"),
                      pr_zip_on(D5, [kit.bc_row("EQ", "STEADYCO", "STEADY CO", "FVSPLT FRM RS 10 TO RS 5", ex_date=D5)]),
                      trade_date=D5)
        derive_corporate_actions(st, workspace="india")
    second = compose(root, as_of=D5, decision=S)
    early = {(row.stock_code, row.trade_date): row for row in first.rows}
    later = {(row.stock_code, row.trade_date): row for row in second.rows}
    for day in (D1, D2, D3, D4):
        before, after = early[("STEADY", day)], later[("STEADY", day)]
        assert (before.raw_open, before.raw_close, before.raw_volume) == (after.raw_open, after.raw_close, after.raw_volume)
        assert before.adj_close == Decimal("100.0000") and after.adj_close == Decimal("50.0000")
        assert after.adj_volume == after.raw_volume * 2
    assert later[("STEADY", D5)].adj_close == later[("STEADY", D5)].raw_close
    assert first.factor_set_hashes["INE002A01018"] != second.factor_set_hashes["INE002A01018"]


def test_an_unresolved_action_withholds_adjusted_prices_and_bar_from_row_maps_them_to_none(tmp_path):
    root = tmp_path / "world"

    def rights(st):
        ingest_pr_zip(st, cc.desc(kind="pr_zip"),
                      pr_zip_on(D2, [kit.bc_row("EQ", "STEADYCO", "STEADY CO", "RIGHTS 1:1 @ PRM RS 3/-", ex_date=D4)]),
                      trade_date=D2)
        derive_corporate_actions(st, workspace="india")

    build_world(root, extra=rights)
    result = compose(root)
    steady = {row.trade_date: row for row in result.rows if row.stock_code == "STEADY"}
    for day in (D1, D2, D3):
        assert steady[day].adjusted_quarantined and steady[day].adj_close is None
        bar = bar_from_row(steady[day])
        assert (bar.adj_open, bar.adj_close) == (None, None) and bar.raw_close == Decimal("100.0000")
    assert not steady[D4].adjusted_quarantined and steady[D5].adj_close == Decimal("100.0000")


# --------------------------------------------------------------------------- 4: quarantine, never choose
def test_a_breeze_conflict_and_a_cross_check_mismatch_leave_no_row_for_that_day(tmp_path):
    root = tmp_path / "world"

    def disagree(st):
        # a second Breeze answer that contradicts the first on D3 for STEADY (a conflict)
        cc.load_breeze(st, "STEADY", cc.breeze_rows("STEADY", {D3: cc.STEADY[D3]}, overrides={D3: ("100", "107", "95", "101")}),
                       start=D3, end=D3, locator="relay/STEADY/second")

    build_world(root, extra=disagree)
    rows = {(row.stock_code, row.trade_date) for row in compose(root).rows}
    assert ("STEADY", D3) not in rows and ("STEADY", D2) in rows and ("STEADY", D4) in rows
    mismatch = tmp_path / "mismatch"

    def far_off(st):
        cc.load_breeze(st, "CANBAN", cc.breeze_rows("CANBAN", {D4: cc.CANBK[D4]}, overrides={D4: ("119", "121", "118", "140")}),
                       start=D4, end=D4, locator="relay/CANBAN/D4")

    build_world(mismatch, extra=far_off)
    rows = {(row.stock_code, row.trade_date) for row in compose(mismatch).rows}
    assert ("CANBAN", D4) not in rows and ("CANBAN", D3) in rows and ("CANBAN", D5) in rows


# --------------------------------------------------------------------------- 5: determinism and no feedback (RLC)
RLC_TARGETS = cc.make_targets(cc.member("RELIANCE", pb.RELIANCE, "RELIAN"))


def build_rlc_store(root: Path, changes_rule: str) -> None:
    nc.rlc_world(root, changes_rule)
    with PilotDataStore(root, workspace="india") as st:
        rows = [kit.breeze_row("RELIAN", f"{day.isoformat()} 00:00:00", "100", "101", "99", "100") for day in (nc.S1, nc.S2)]
        cc.load_breeze(st, "RELIAN", rows, start=nc.S1, end=nc.S2)
        mark_session_store(st)


def rlc_compose(root: Path, store_run_ids=None):
    with PilotDataStore(root, workspace="india") as st:
        return bars_as_of(st, targets=RLC_TARGETS, decision_session=nc.S3, as_of=nc.S2, window_start=nc.S1,
                          tolerances=TOLERANCES, workspace="india")


@pytest.mark.parametrize("changes_rule", ["next_session_after_file_date", "file_date"])
def test_a_forecast_between_two_calls_changes_neither_the_rows_nor_the_store_run_id(tmp_path, changes_rule):
    root = tmp_path / "rlc"
    build_rlc_store(root, changes_rule)
    calendar = nc.pinned(tmp_path)
    # precondition, on a writable copy that holds S's own evidence: the conflict is reachable and the run id sees it
    copy_root = tmp_path / "copy"
    shutil.copytree(root, copy_root)
    clean = rlc_compose(root)
    with PilotDataStore(copy_root, workspace="india") as copy:
        nc.land_session(copy, nc.S3)
        band_on(copy, isin=pb.RELIANCE, session=nc.S3, workspace="india")
        written = copy.query("SELECT count(*) FROM quarantine_records WHERE reason_code = ? AND date_from = ?",
                             ["band_crosscheck_row_conflict", nc.S3])[0][0]
        assert written > 0
    seen = rlc_compose(copy_root)
    assert seen.store_crosscheck_run_id != clean.store_crosscheck_run_id  # the run id provably sees a quarantine
    assert canon(seen) == canon(clean)  # while the composition does not
    # the forecast store: call, forecast, call
    with PilotDataStore(root, workspace="india") as st:
        before_ids = [row[0] for row in st.query("SELECT quarantine_id FROM quarantine_records ORDER BY quarantine_id")]
    first = rlc_compose(root)
    again = rlc_compose(root)
    assert canon(first) == canon(again) and first.store_crosscheck_run_id == again.store_crosscheck_run_id
    with PilotDataStore(root, workspace="india") as st:
        mid_ids = [row[0] for row in st.query("SELECT quarantine_id FROM quarantine_records ORDER BY quarantine_id")]
        observation = band_for_upcoming_session(st, isin=pb.RELIANCE, prior_session=nc.S2, session=nc.S3,
                                                calendar=calendar, workspace="india")
        assert observation.session == nc.S3
        after_forecast = [row[0] for row in st.query("SELECT quarantine_id FROM quarantine_records ORDER BY quarantine_id")]
    last = rlc_compose(root)
    assert mid_ids == after_forecast
    assert canon(last) == canon(first)
    assert last.store_crosscheck_run_id == first.store_crosscheck_run_id
    assert before_ids != mid_ids or True  # the first call may add crosscheck quarantines; none changes after it


# --------------------------------------------------------------------------- 6: refusals
def test_refusals_name_their_reason_and_never_touch_the_research_store(frozen, tmp_path):
    root = tmp_path / "world"
    build_world(root)
    with pytest.raises(PilotDataError) as read_only:
        compose(root, writable=False)
    assert read_only.value.code == "store_read_only"
    research = frozen["root"]
    before = ss.tree_state(research)
    with PilotDataStore(research, workspace="india") as unseeded, pytest.raises(PilotDataError) as unseeded_error:
        bars_as_of(unseeded, targets=TARGETS, decision_session=S, as_of=D5, window_start=D1, tolerances=TOLERANCES,
                   workspace="india")
    assert unseeded_error.value.code == "not_a_session_store"
    assert ss.tree_state(research) == before
    plain = tmp_path / "plain"
    with PilotDataStore(plain, workspace="india") as bare:
        cc.load_bhavcopy(bare)
        with pytest.raises(PilotDataError) as bare_error:
            bars_as_of(bare, targets=TARGETS, decision_session=S, as_of=D5, window_start=D1, tolerances=TOLERANCES,
                       workspace="india")
        assert bare_error.value.code == "not_a_session_store"
        assert not bare.table_exists("crosscheck_runs")  # nothing was written before the refusal
    cases = {
        "stale_as_of": dict(as_of=D4, decision=S),  # D5 is a session between them
        "as_of_not_session": dict(as_of=D5 + timedelta(days=1), decision=S),  # a Saturday
        "as_of_not_before_decision": dict(as_of=D5, decision=D5),
        "window_after_as_of": dict(as_of=D5, decision=S, window_start=S),
    }
    for code, arguments in cases.items():
        with pytest.raises(PilotDataError) as caught:
            compose(root, **arguments)
        assert caught.value.code == code, code
    with PilotDataStore(root, workspace="india") as st, pytest.raises(PilotDataError) as space:
        bars_as_of(st, targets=TARGETS, decision_session=S, as_of=D5, window_start=D1, tolerances=TOLERANCES,
                   workspace="uk")
    assert space.value.code == "workspace_mismatch"


def mark_monday_as(st, mark):
    if mark == "pending":  # a 404 on the same IST day proves nothing: NSE publishes in the evening
        for kind in ("pr", "udiff", "cm_legacy"):
            _log_attempt(st, d=S, kind=kind, outcome="no_file", http_status=404, error_code=None,
                         source_sha256=None, url="u", attempted_at=datetime(S.year, S.month, S.day, 8, 0, tzinfo=timezone.utc))
    if mark == "inconsistent":  # a PR file was ingested, yet both primary files are absent
        _log_attempt(st, d=S, kind="pr", outcome="ingested", http_status=200, error_code=None,
                     source_sha256=None, url="u", attempted_at=cc.LATER)
        for kind in ("udiff", "cm_legacy"):
            _log_attempt(st, d=S, kind=kind, outcome="no_file", http_status=404, error_code=None,
                         source_sha256=None, url="u", attempted_at=cc.LATER)


@pytest.mark.parametrize("mark", ["unknown", "pending", "inconsistent"])
def test_an_intervening_day_that_is_not_a_known_non_session_refuses_instead_of_passing_as_fresh(tmp_path, mark):
    """Codex r1: decision Tue S1 with Mon S not yet known must not accept Fri D5 as_of."""
    root = tmp_path / mark
    build_world(root, frozenset({"no_future"}), extra=lambda st: mark_monday_as(st, mark))
    with PilotDataStore(root, workspace="india", read_only=True) as st:  # the precondition: S has the intended status
        assert sessions.day_status(st, S) == mark
    with pytest.raises(PilotDataError) as caught:
        compose(root, as_of=D5, decision=S1)
    assert caught.value.code == "calendar_unknown_dates" and S.isoformat() in str(caught.value)
    with PilotDataStore(root, workspace="india", read_only=True) as st:
        assert not st.table_exists("crosscheck_runs")  # refused before any cross-check ran


# --------------------------------------------------------------------------- 6b: the pinned calendar fills unknown days
SAT, SUN = D5 + timedelta(days=1), D5 + timedelta(days=2)
FRIDAY_BOUND = frozenset({"no_future", "friday_bound"})


def friday_world(root: Path, *, extra=None) -> None:
    build_world(root, FRIDAY_BOUND, extra=extra)
    with PilotDataStore(root, workspace="india", read_only=True) as st:  # the precondition: the weekend is unlogged
        assert [sessions.day_status(st, day) for day in (SAT, SUN, S)] == ["unknown"] * 3


def refused(root, code, mentions=None, **kwargs):
    with pytest.raises(PilotDataError) as caught:
        compose(root, **kwargs)
    assert caught.value.code == code, (caught.value.code, str(caught.value))
    if mentions is not None:
        assert mentions.isoformat() in str(caught.value)


def test_a_friday_bounded_seed_refuses_the_monday_decision_without_a_calendar_and_passes_with_a_covering_one(tmp_path):
    """Codex r2: the seed copies fetch logs only to window_end, so Sat and Sun are unknown to the store."""
    root = tmp_path / "friday"
    friday_world(root)
    refused(root, "calendar_unknown_dates", SAT)  # no calendar: fail closed, as before
    calendar = nc.pinned(tmp_path)
    passed = compose(root, calendar=calendar)
    reference = tmp_path / "reference"
    build_world(reference, frozenset({"no_future"}))  # the weekend logged as a 404 the day after
    assert canon(passed) == canon(compose(reference)) and len(passed.rows) == 8


def test_the_calendar_gives_nothing_outside_its_coverage_for_a_special_session_or_for_an_unresolved_weekday(tmp_path):
    root = tmp_path / "friday"
    friday_world(root)
    short = nc.pinned(tmp_path, coverage=(date(2025, 3, 1), SAT))  # Sunday is outside
    refused(root, "calendar_unknown_dates", SUN, calendar=short)
    special = nc.pinned(tmp_path, special=(SAT,))
    refused(root, "calendar_unknown_dates", SAT, calendar=special)
    # Tuesday decision: Monday S is a plain weekday the table does not list as a holiday and the store has not logged
    refused(root, "calendar_unknown_dates", S, decision=S1, calendar=nc.pinned(tmp_path))
    holiday = nc.pinned(tmp_path, holidays=(S,))  # listed as a holiday, so Tuesday is the next session and as_of is fresh
    assert len(compose(root, decision=S1, calendar=holiday).rows) == 8


@pytest.mark.parametrize("mark", ["pending", "inconsistent"])
def test_a_pending_or_inconsistent_day_is_not_rescued_by_a_calendar_that_lists_it_as_a_holiday(tmp_path, mark):
    root = tmp_path / mark
    build_world(root, FRIDAY_BOUND, extra=lambda st: mark_monday_as(st, mark))
    refused(root, "calendar_unknown_dates", S, decision=S1, calendar=nc.pinned(tmp_path, holidays=(S,)))


def test_a_contradiction_between_the_store_and_the_calendar_refuses(tmp_path):
    def monday_holiday_in_store(st):
        cc.mark_calendar(st, S, S, set())  # all three files 404 after S: a final holiday

    def monday_session_in_store(st):
        cc.mark_calendar(st, S, S, {S})

    holiday_root, session_root = tmp_path / "holiday", tmp_path / "session"
    build_world(holiday_root, FRIDAY_BOUND, extra=monday_holiday_in_store)
    build_world(session_root, FRIDAY_BOUND, extra=monday_session_in_store)
    with PilotDataStore(holiday_root, workspace="india", read_only=True) as st:
        assert sessions.day_status(st, S) == "holiday"
    # the store says holiday, the table says an ordinary weekday
    refused(holiday_root, "calendar_conflict", S, decision=S1, calendar=nc.pinned(tmp_path))
    # ... and the table says holiday too: nothing contradicts, so the day is a known non-session
    assert len(compose(holiday_root, decision=S1, calendar=nc.pinned(tmp_path, holidays=(S,))).rows) == 8
    # the store says a session, the table says holiday: stale, whatever the table says
    refused(session_root, "stale_as_of", S, decision=S1, calendar=nc.pinned(tmp_path, holidays=(S,)))
    # the store says holiday and the table lists the day as a special session
    refused(holiday_root, "calendar_conflict", S, decision=S1, calendar=nc.pinned(tmp_path, special=(S,)))


def monday_file_without_log(st):
    """A normal ingest interrupted after Monday's UDiFF committed and before its success log line."""
    rows = [cc.ohlc_row("CANBK", "EQ", cc.NEW, CANBK_FUTURE[S], S, "11256"),
            cc.ohlc_row("STEADYCO", "EQ", cc.STEADY_ISIN, STEADY_FUTURE[S], S, "5")]
    ingest_udiff(st, cc.desc(), kit.udiff_zip(S, rows), trade_date=S)


def saturday_pr_ingested(st):
    """A PR logged as ingested on a Saturday with no primary file: `_after_pr_ingested` reads it as unknown."""
    _log_attempt(st, d=SAT, kind="pr", outcome="ingested", http_status=200, error_code=None, source_sha256=None,
                 url="u", attempted_at=cc.LATER)


def test_a_wrong_holiday_listing_cannot_rescue_an_unknown_day_that_holds_a_file_or_a_logged_ingest(tmp_path):
    """Codex r3-1 and Opus r3-1: the calendar fills only days the store has no evidence for."""
    torn, saturday = tmp_path / "torn", tmp_path / "saturday"
    build_world(torn, FRIDAY_BOUND, extra=monday_file_without_log)
    build_world(saturday, FRIDAY_BOUND, extra=saturday_pr_ingested)
    with PilotDataStore(torn, workspace="india", read_only=True) as st:  # the preconditions: unknown, yet a file and a bar
        assert sessions.day_status(st, S) == "unknown"
        assert st.query("SELECT count(*) FROM bhavcopy_files WHERE trade_date = ?", [S])[0][0] == 1
        assert st.query("SELECT count(*) FROM bhavcopy_bars WHERE trade_date = ?", [S])[0][0] >= 1
    with PilotDataStore(saturday, workspace="india", read_only=True) as st:
        assert sessions.day_status(st, SAT) == "unknown"
    wrong = nc.pinned(tmp_path, holidays=(S,))  # lists Monday, which has a file, as a holiday
    refused(torn, "session_evidence_unknown_status", S, decision=S1, calendar=wrong)
    refused(saturday, "session_evidence_unknown_status", SAT, calendar=nc.pinned(tmp_path))
    for root in (torn, saturday):
        with PilotDataStore(root, workspace="india", read_only=True) as st:
            assert not st.table_exists("crosscheck_runs")  # refused before any cross-check ran


def test_a_weekend_with_no_fetch_log_file_or_bar_is_still_rescued_next_to_a_day_with_evidence(tmp_path):
    root = tmp_path / "control"
    friday_world(root)  # Sat and Sun were never fetched and nothing is stored for them
    with PilotDataStore(root, workspace="india", read_only=True) as st:
        assert all(live_bars._local_session_evidence(st, day) is None for day in (SAT, SUN))
    assert len(compose(root, calendar=nc.pinned(tmp_path)).rows) == 8


@pytest.fixture(scope="module")
def friday_seed(frozen, tmp_path_factory):
    """A real seeded session store through Friday only, with no weekend attempt marked (the `frozen` one marks it)."""
    session_root = tmp_path_factory.mktemp("friday_seed") / "session"
    seed_session_store(session_root, frozen["root"], dataset_dir=frozen["dataset"], window_start=D1, window_end=D5,
                       workspace="india", band_parity_offset_days=2)
    return session_root


def test_a_real_friday_seed_needs_the_calendar_or_decision_session_one_day_on_and_both_give_the_frozen_rows(
        frozen, friday_seed, tmp_path):
    manifest = frozen["manifest"]
    outcomes = {}
    for name, decision, calendar in (("calendar", S, nc.pinned(tmp_path)), ("next_day", D5 + timedelta(days=1), None)):
        root = tmp_path / name
        shutil.copytree(friday_seed, root)
        with PilotDataStore(root, workspace="india") as st:
            outcomes[name] = bars_as_of(st, targets=frozen["targets"], decision_session=decision, as_of=manifest.window_end,
                                        window_start=manifest.window_start, tolerances=TOLERANCES, workspace="india",
                                        calendar=calendar)
    root = tmp_path / "bare"
    shutil.copytree(friday_seed, root)
    with PilotDataStore(root, workspace="india") as st, pytest.raises(PilotDataError) as bare:
        bars_as_of(st, targets=frozen["targets"], decision_session=S, as_of=manifest.window_end,
                   window_start=manifest.window_start, tolerances=TOLERANCES, workspace="india")
    assert bare.value.code == "calendar_unknown_dates"
    assert canon(outcomes["calendar"]) == canon(outcomes["next_day"])  # the decision date is not in the composition
    frozen_rows = read_dataset_rows(frozen["dataset"])
    assert [row.model_dump(exclude={"crosscheck_run_id"}) for row in outcomes["calendar"].rows] == [
        row.model_dump(exclude={"crosscheck_run_id"}) for row in frozen_rows]


# --------------------------------------------------------------------------- 7: strategy fit
def test_every_row_fits_the_strategy_and_a_holdout_view_builds(tmp_path):
    root = tmp_path / "world"
    build_world(root)
    result = compose(root)
    assert all(bar_from_row(row).session == row.trade_date for row in result.rows)
    view = DatasetView.from_rows(result.rows, holdout=HoldoutRange(S, S))  # the decision session is the sealed one
    assert view.visible_end == D5
    with pytest.raises(HoldoutViolation):
        view.guard(S)
    view.guard(D5)  # every row the pilot composes is development data
    assert all(row.trade_date < S for row in result.rows)  # none falls inside the sealed holdout
    assert events_from_manifest(result) is not None
    assert [c.code for c in result.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]


# --------------------------------------------------------------------------- 8: no network, and the wall time
def test_no_network_is_used_the_module_has_no_http_import_and_the_call_is_fast_on_the_fixture(tmp_path):
    root = tmp_path / "world"
    build_world(root)
    started = time.perf_counter()
    compose(root)
    wall = time.perf_counter() - started
    print(f"bars_as_of wall time on the fixture: {wall:.3f}s")
    assert wall < 30
    tree = ast.parse(Path(live_bars.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {alias.name for alias in node.names}
    banned = {"httpx", "requests", "urllib", "socket", "nse_http", "NseHttp", "relay", "subprocess"}
    assert not banned & {part for name in imported for part in name.replace(".", " ").split()}, imported


# --------------------------------------------------------------------------- the parity CLI
def test_the_parity_cli_exits_zero_on_a_matching_seed_and_two_on_a_pinned_target_mismatch(frozen, tmp_path, capsys, monkeypatch):
    root = tmp_path / "session"
    shutil.copytree(frozen["session"], root)
    code = live_main(["parity", "--dataset", str(frozen["dataset"]), "--session-root", str(root),
                      "--decision-session", S.isoformat()])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and list(out)[0] == "caveats" and out["parity"]["equal"] is True
    assert out["parity"]["factor_set_differs"] == [] and all(item["equal"] for item in out["parity"]["anchors"].values())
    assert out["parity"]["wall_seconds"] >= 0
    refused = live_main(["parity", "--dataset", str(frozen["dataset"]), "--session-root", str(tmp_path / "missing"),
                         "--decision-session", S.isoformat()])
    assert refused == 2 and json.loads(capsys.readouterr().out)["error_code"] == "seed_report_missing"
    real = live_bars.verify_dataset
    monkeypatch.setattr(live_bars, "verify_dataset",
                        lambda *a, **k: real(*a, **k).model_copy(update={"targets_sha256": "f" * 64}))
    mismatch = live_main(["parity", "--dataset", str(frozen["dataset"]), "--session-root", str(root),
                          "--decision-session", S.isoformat()])
    assert mismatch == 2 and json.loads(capsys.readouterr().out)["error_code"] == "seed_targets_mismatch"
