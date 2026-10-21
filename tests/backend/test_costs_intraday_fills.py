"""Intraday limit-fill model: one row per 64-INTRADAY-DESIGN rule (R-1 to R-33) and invariant (I-1 to I-5).

All prices, bars, volumes and bhavcopy figures are SYNTHETIC test values. Rows are named after the
design's section 14 map so a review can walk rule by rule.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from costs import intraday_fills as ifs
from costs.cas_class import CasClassRow
from costs.core import IST, InputError, ScheduleError, Side, seal, sha256_hex
from costs.fills import (
    FILLED_AT_LIMIT,
    MISSED_LOCKED_AT_BAND,
    MISSED_THRESHOLD,
    MISSED_VOLUME_CAP_ZERO,
    NO_FILL_AMBIGUOUS_SINGLE_PRICE,
    NO_FILL_BAND_DATE_MISMATCH,
    NO_FILL_BAND_UNAVAILABLE,
    PARTIAL_VOLUME_CAP,
    REJECTED_OFF_TICK,
    REJECTED_OUTSIDE_BAND,
    BandUnavailable,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
    load_fill_scenarios,
    simulate_session,
)
from costs.intraday_fills import (
    AdmittedBars,
    BhavcopyDay,
    IntradayAttempt,
    IntradayEvidence,
    IntradayOutcome,
    IntradayRefusal,
    MinuteBar,
    SessionFacts,
    admit_bars,
    assess_day,
    load_intraday_scenarios,
    no_evidence_results,
    parse_intraday_scenarios,
    simulate_intraday,
    verify_day_integrity,
)

D = Decimal
ISIN = "INE0TEST0001"
SERIES = "EQ"
CODE = "TESTCO"
SESSION = date(2026, 10, 6)
NEXT_SESSION = date(2026, 10, 7)
SCHEDULE_PATH = (
    Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "intraday_bar_fill_scenarios.json"
)
SCHEDULE_TEXT = SCHEDULE_PATH.read_text(encoding="utf-8")
SCHEDULE = load_intraday_scenarios()
EXPECTED_SCHEDULE_HASH = "c057f9c8346c7664722a817cc2ed404739cc666b0f8c0ffbb6da5664303444bb"

TICK = TickSize(D("0.05"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
FIXED_BAND = PriceBand("fixed", D("360.00"), D("440.00"), SESSION, "test-band", sha256_hex("test-band"))
NO_BAND = PriceBand("no_band", None, None, SESSION, "test-band", sha256_hex("test-band"))
SESSION_OK = SessionFacts(True, SCHEDULE.session_timing_version)


def cas_row(cas_class: str = "cas", isin: str = ISIN, session: date = SESSION) -> CasClassRow:
    return CasClassRow(
        isin, CODE, cas_class, session, "cas-test-v1", "r1", sha256_hex("version"), "PUB-TEST-1",
        date(2026, 10, 1), sha256_hex("saved-file"), date(2026, 10, 2),
    )


# ---- builders ----------------------------------------------------------------------------


def at(hhmm: str, seconds: int = 0) -> datetime:
    return datetime.combine(SESSION, time.fromisoformat(hhmm), tzinfo=IST) + timedelta(seconds=seconds)


def label(hhmm: str) -> str:
    return f"{SESSION.isoformat()} {hhmm}:00"


def hhmm_range(first: str = "09:15", last: str = "15:29") -> list[str]:
    moment, end, out = at(first), at(last), []
    while moment <= end:
        out.append(moment.strftime("%H:%M"))
        moment += timedelta(minutes=1)
    return out


def row(hhmm: str, *, low="399.80", high="400.20", open_="400.00", close="400.00", vol=1000, session=SESSION):
    return MinuteBar(f"{session.isoformat()} {hhmm}:00", D(open_), D(high), D(low), D(close), vol)


def session_rows(_base=None, **overrides) -> list[MinuteBar]:
    """A full 09:15 to 15:29 session of quiet bars. Keys like ``t1130`` override that minute."""
    out = []
    for hhmm in hhmm_range():
        spec = overrides.get("t" + hhmm.replace(":", ""), {})
        if spec is None:
            continue  # the bar is absent from the payload
        out.append(row(hhmm, **{**(_base or {}), **spec}))
    return out


# Quiet bars that sit at the upper band edge without crossing a 440.00 buy (threshold 439.95).
NEAR_UPPER = {"low": "439.96", "high": "440.00", "open_": "440.00", "close": "440.00"}


CROSS_ALL_BUY = {"low": "399.30", "open_": "399.60", "close": "399.60", "high": "399.70", "vol": 5000}
CROSS_ALL_SELL = {"high": "400.80", "open_": "400.30", "close": "400.30", "low": "400.20", "vol": 5000}


def evidence_for(rows, **kw) -> IntradayEvidence:
    values = dict(
        stock_code=CODE,
        session_date=SESSION,
        interval="1minute",
        source_sha256=sha256_hex("payload-" + str(len(rows)) + (rows[0].label if rows else "")),
        sha256_verified=True,
        fetched_at=at("16:00"),
        route="/v1/bars/intraday",
        request_params=(("interval", "1minute"),),
        empty_flag=False,
        bars=tuple(rows),
        next_session_date=NEXT_SESSION,
    )
    values.update(kw)
    return IntradayEvidence(**values)


def bhavcopy_for(rows, *, total=None, series=SERIES) -> BhavcopyDay:
    window = [r for r in rows if "09:15" <= r.label[11:16] <= "15:35"]
    return BhavcopyDay(
        ISIN, series, SESSION,
        sum(r.volume for r in window) if total is None else total,
        max(r.high for r in window), min(r.low for r in window),
    )


def assess(rows=None, *, schedule=SCHEDULE, evidence=None, bhavcopy="auto", cas=None, session=SESSION_OK,
           band=FIXED_BAND, daily=None, tick=TICK, **evidence_kw):
    rows = session_rows() if rows is None else rows
    return assess_day(
        evidence if evidence is not None else evidence_for(rows, **evidence_kw),
        schedule=schedule, isin=ISIN, series=SERIES, stock_code=CODE, session_date=SESSION, tick=tick, band=band,
        session=session, cas_row=cas_row() if cas is None else cas,
        bhavcopy=bhavcopy_for(rows) if bhavcopy == "auto" else bhavcopy, daily_bar_volume=daily,
    )


def attempt(intent_id="a1", *, side=Side.BUY, quantity=10, limit="399.50", at_time="10:02", seconds=30,
            band=FIXED_BAND, tick=TICK, schedule=SCHEDULE) -> IntradayAttempt:
    return IntradayAttempt(
        intent_id, ISIN, SERIES, CODE, SESSION, side, quantity, D(limit), tick, band,
        at(at_time, seconds), schedule.schedule_hash,
    )


def run(rows=None, attempts=None, *, schedule=SCHEDULE, band=FIXED_BAND, **kw):
    rows = session_rows() if rows is None else rows
    attempts = [attempt(schedule=schedule, band=band)] if attempts is None else attempts
    day = assess(rows, schedule=schedule, band=band, **kw)
    return simulate_intraday(day, attempts, schedule)


def scenario_text(**changes) -> str:
    raw = json.loads(SCHEDULE_TEXT)
    raw.update(changes)
    return json.dumps(raw)


def schedule_with(**changes):
    return parse_intraday_scenarios(scenario_text(**changes), "test")


NEXT_DAY = {"rule": "next_day_exception_v1", "window_start_ist": "08:00", "window_end_ist": "09:25", "max_calls": 40}


def final_schedule(cas_lower="0.90", non_cas_lower="0.90", **extra):
    return schedule_with(
        lower_bound_status="final",
        volume_ratio_band={
            "cas": {"lower": cas_lower, "upper": "1.02"},
            "non_cas": {"lower": non_cas_lower, "upper": "1.02"},
        },
        **extra,
    )


def total_rows(total: int) -> list[MinuteBar]:
    """A full session whose minute volume sums to exactly ``total``."""
    rows = session_rows()
    each, extra = divmod(total, len(rows))
    return [dataclasses.replace(r, volume=each + (extra if n == 0 else 0)) for n, r in enumerate(rows)]


def by_k(result):
    return {s.k_ticks: s for s in result.scenarios}


def code_of(result):
    return result.inconclusive_code


# ---- the committed schedule --------------------------------------------------------------


def test_committed_schedule_hash_is_pinned():
    # Any change to the committed file needs a new version id and a new literal here, in the same commit.
    assert SCHEDULE.schedule_hash == EXPECTED_SCHEDULE_HASH
    assert SCHEDULE.version == "intraday-bar-limit-fills-2026-10-22.r1"


def test_schedule_v1_pins_the_approved_values():
    assert SCHEDULE.interval == "1minute"
    assert SCHEDULE.lifetime_end == time(15, 15)
    assert SCHEDULE.label_semantics == "unproven"
    assert SCHEDULE.label_evidence_ref is None and SCHEDULE.label_approval_ref is None
    assert SCHEDULE.gap_policy == "inconclusive"
    assert SCHEDULE.lower_bound_status == "provisional"
    for band in (SCHEDULE.cas_band, SCHEDULE.non_cas_band):
        assert band.upper == D("1.02") and band.lower is None
    assert SCHEDULE.next_day_fetch is None
    assert (SCHEDULE.earliest_fetch, SCHEDULE.latest_fetch) == (time(15, 40), time(23, 30))
    assert [(s.scenario_id, s.k_ticks, s.volume_participation) for s in SCHEDULE.scenarios] == [
        ("base", 1, D("0.01")), ("adverse", 2, D("0.005")), ("pessimistic", 3, D("0.0025")),
    ]


def test_schedule_matches_the_daily_k_and_participation():
    daily = load_fill_scenarios().all()
    assert [(s.k_ticks, s.volume_participation) for s in SCHEDULE.scenarios] == [
        (s.k_ticks, s.volume_participation) for s in daily
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "growin.costs.intraday_fill_scenarios/2"},
        {"interval": "1day"},
        {"interval": "5minute"},
        {"gap_policy": "zero_volume"},
        {"lower_bound_status": "final"},  # final with null lowers
        {"lower_bound_status": "other"},
        {"label_semantics": "likely"},
        {"volume_ratio_band": {"cas": {"lower": "0.9", "upper": "1.02"}, "non_cas": {"lower": None, "upper": "1.02"}}},
        {"volume_ratio_band": {"cas": {"lower": None, "upper": 1.02}, "non_cas": {"lower": None, "upper": "1.02"}}},
        {"day_check_window_ist": ["15:35", "09:15"]},
        {"earliest_fetch_ist": "23:45"},
        {"next_day_fetch": {"rule": "other", "window_start_ist": "08:00", "window_end_ist": "09:25", "max_calls": 40}},
        {"scenarios": []},
        {"scenarios": [{"id": "a", "k_ticks": 1, "volume_participation": "0.01"},
                       {"id": "b", "k_ticks": 1, "volume_participation": "0.01"}]},
        {"label_approval_ref": "not-a-hash"},
        {"unknown_key": "x"},
    ],
)
def test_schedule_loader_refuses(changes):
    with pytest.raises(ScheduleError):
        schedule_with(**changes)


@pytest.mark.parametrize("upper", ["1.03", "1.021", "1.0201", "1.01", "1", "2", "0.5", "1.020001"])
@pytest.mark.parametrize("cas_class", ["cas", "non_cas"])
def test_R15_loader_refuses_an_upper_bound_other_than_1_02(cas_class, upper):
    """D-12: the upper bound is fixed. A schedule that moves it never loads, so a 1.021 ratio cannot fill."""
    bands = {"cas": {"lower": None, "upper": "1.02"}, "non_cas": {"lower": None, "upper": "1.02"}}
    bands[cas_class] = {"lower": None, "upper": upper}
    with pytest.raises(ScheduleError, match="D-12"):
        schedule_with(volume_ratio_band=bands)
    # Same refusal on a final schedule with numeric lowers.
    with pytest.raises(ScheduleError, match="D-12"):
        schedule_with(
            lower_bound_status="final",
            volume_ratio_band={
                **{k: {"lower": "0.90", "upper": "1.02"} for k in bands},
                cas_class: {"lower": "0.90", "upper": upper},
            },
        )


def test_R15_loader_accepts_the_fixed_upper_bound_in_any_spelling_of_1_02():
    assert schedule_with(
        volume_ratio_band={"cas": {"lower": None, "upper": "1.020"}, "non_cas": {"lower": None, "upper": "1.02"}}
    ).cas_band.upper == D("1.02")


@pytest.mark.parametrize(
    "changes",
    [
        {"window_start_ist": "00:00", "window_end_ist": "23:59", "max_calls": 4000},  # the codex-r1-2 schedule
        {"window_start_ist": "00:00"},
        {"window_start_ist": "07:59"},
        {"window_end_ist": "09:26"},
        {"window_end_ist": "18:00"},
        {"window_end_ist": "23:59"},
        {"max_calls": 41},
        {"max_calls": 4000},
        {"max_calls": 0},
        {"max_calls": True},
        {"max_calls": "40"},
        {"window_start_ist": "09:00", "window_end_ist": "09:00"},
    ],
)
def test_R18_loader_refuses_a_next_day_fetch_outside_d27(changes):
    with pytest.raises(ScheduleError):
        schedule_with(next_day_fetch={**NEXT_DAY, **changes})


def test_R18_loader_accepts_a_next_day_fetch_inside_d27_and_narrower():
    assert schedule_with(next_day_fetch=NEXT_DAY).next_day_fetch is not None
    narrow = schedule_with(next_day_fetch={**NEXT_DAY, "window_start_ist": "08:30", "window_end_ist": "09:00", "max_calls": 20})
    assert narrow.next_day_fetch.max_calls == 20


def test_R18_an_out_of_window_next_day_fetch_does_not_fill():
    """codex-r1-2: with the window held to 08:00 to 09:25, an 18:00 next-day fetch is INCONCLUSIVE, never a fill."""
    sched = schedule_with(next_day_fetch=NEXT_DAY)
    rows = session_rows(t1130=CROSS_ALL_BUY)
    for hour, minute in ((18, 0), (9, 26), (7, 59), (12, 0), (23, 59)):
        moment = datetime.combine(NEXT_SESSION, time(hour, minute), tzinfo=IST)
        (result,) = run(rows, [attempt(schedule=sched)], schedule=sched, fetched_at=moment)
        assert code_of(result) == ifs.EVIDENCE_FETCHED_TOO_LATE, moment
        assert all(s.outcome is IntradayOutcome.INCONCLUSIVE and s.filled_quantity == 0 for s in result.scenarios)
    # The schedule that would have let 18:00 through never loads.
    with pytest.raises(ScheduleError):
        schedule_with(next_day_fetch={**NEXT_DAY, "window_start_ist": "00:00", "window_end_ist": "23:59", "max_calls": 4000})


def test_final_schedule_without_numeric_lower_is_refused_and_with_one_loads():
    with pytest.raises(ScheduleError):
        schedule_with(lower_bound_status="final")
    assert final_schedule().lower_bound_status == "final"
    assert final_schedule().cas_band.lower == D("0.90")


def test_schedule_loader_refuses_duplicate_keys_and_floats():
    with pytest.raises(ScheduleError):
        parse_intraday_scenarios(SCHEDULE_TEXT.replace('"interval": "1minute",', '"interval": "1minute", "interval": "1minute",'))
    with pytest.raises(ScheduleError):
        parse_intraday_scenarios(SCHEDULE_TEXT.replace('"upper": "1.02"', '"upper": 1.02', 1))


# ---- R-1 input pin -----------------------------------------------------------------------


def test_R1_input_pin_missing():
    base = dict(
        intent_id="a1", isin=ISIN, series=SERIES, stock_code=CODE, session_date=SESSION, side=Side.BUY,
        quantity=10, limit_price=D("399.50"), tick=TICK, band=FIXED_BAND, submitted_at=at("10:02"),
        scenario_set_hash=SCHEDULE.schedule_hash,
    )
    IntradayAttempt(**base)
    for field, bad in [
        ("quantity", 0), ("quantity", True), ("quantity", 1.5), ("limit_price", 399.5), ("limit_price", "399.5"),
        ("side", "BUY"), ("tick", None), ("band", None), ("intent_id", ""), ("scenario_set_hash", "abc"),
        ("submitted_at", None),
    ]:
        with pytest.raises(InputError):
            IntradayAttempt(**{**base, field: bad})


def test_R1_input_naive_time():
    with pytest.raises(InputError):
        attempt_naive = dict(
            intent_id="a1", isin=ISIN, series=SERIES, stock_code=CODE, session_date=SESSION, side=Side.BUY,
            quantity=10, limit_price=D("399.50"), tick=TICK, band=FIXED_BAND,
            submitted_at=datetime(2026, 10, 6, 10, 2), scenario_set_hash=SCHEDULE.schedule_hash,
        )
        IntradayAttempt(**attempt_naive)


def test_R1_submission_on_another_date_is_an_input_error():
    with pytest.raises(InputError):
        IntradayAttempt(
            "a1", ISIN, SERIES, CODE, SESSION, Side.BUY, 10, D("399.50"), TICK, FIXED_BAND,
            at("10:02") - timedelta(days=1), SCHEDULE.schedule_hash,
        )


def test_R1_other_timezones_are_read_as_instants():
    utc = timezone.utc
    moment = at("10:02", 30).astimezone(utc)
    same = IntradayAttempt("a1", ISIN, SERIES, CODE, SESSION, Side.BUY, 10, D("399.50"), TICK, FIXED_BAND, moment,
                           SCHEDULE.schedule_hash)
    (result,) = simulate_intraday(assess(), [same], SCHEDULE)
    assert {e.reason: e for e in result.exclusions}["before_submission"].last_label == label("10:03")


def test_attempt_with_another_scenario_hash_is_refused():
    other = dataclasses.replace(attempt(), scenario_set_hash=sha256_hex("other"))
    with pytest.raises(InputError):
        simulate_intraday(assess(), [other], SCHEDULE)


# ---- R-2 / R-3 / R-4 evidence envelope and opaque input ----------------------------------


@pytest.mark.parametrize("kw", [{"stock_code": "OTHERCO"}, {"session_date": date(2026, 10, 5)}])
def test_R2_scope_mismatch(kw):
    rows = session_rows()
    (result,) = simulate_intraday(assess(rows, evidence=evidence_for(rows, **kw)), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.EVIDENCE_SCOPE_MISMATCH


@pytest.mark.parametrize("interval", ["1day", "5minute", "30minute", "1second", "1Minute"])
def test_R3_interval_not_approved(interval):
    rows = session_rows(t1130=CROSS_ALL_BUY)
    (result,) = simulate_intraday(assess(rows, interval=interval), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.INTERVAL_NOT_APPROVED
    assert all(s.filled_quantity == 0 and s.outcome is IntradayOutcome.INCONCLUSIVE for s in result.scenarios)


def test_R3_1day_bar_never_reaches_a_fill_even_with_a_crossing_low():
    day_bar = [MinuteBar(label("00:00"), D("400"), D("401"), D("390"), D("400"), 10**7)]
    (result,) = simulate_intraday(assess(day_bar, interval="1day", bhavcopy=None), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.INTERVAL_NOT_APPROVED


def test_R4_admitted_bars_only():
    day = assess()
    with pytest.raises(InputError):  # cannot be built by hand
        AdmittedBars("a1", day.source_sha256, day.day_hash, at("10:02"), at("15:15"), timedelta(minutes=1), (), (), "", "")
    admitted = admit_bars(day, attempt(), SCHEDULE)
    assert isinstance(admitted, AdmittedBars)
    assert all(bar.start - admitted.interval >= admitted.submitted_at for bar in admitted.bars)


def test_R4_pre_submission_bar_offered(monkeypatch):
    rows = session_rows(t1003={"low": "399.00", "vol": 9000})
    day = assess(rows)
    key = ifs._ADMIT_KEY
    early = [bar for bar in day.bars if bar.label == label("10:03")]
    with pytest.raises(IntradayRefusal) as raised:
        AdmittedBars("a1", day.source_sha256, day.day_hash, at("10:02", 30), at("15:15"), timedelta(minutes=1),
                     tuple(early), (), "10:04", "15:14", _key=key)
    assert raised.value.code == ifs.PRE_SUBMISSION_BAR_OFFERED
    # A faulty admission rule is caught by the constructor re-check and surfaces as INCONCLUSIVE.
    monkeypatch.setattr(ifs, "_admissible", lambda start, submitted, end, interval: True)
    (result,) = simulate_intraday(day, [attempt()], SCHEDULE)
    assert code_of(result) == ifs.PRE_SUBMISSION_BAR_OFFERED
    assert all(s.filled_quantity == 0 for s in result.scenarios)


def test_R33_admitted_bars_require_passing_day_integrity():
    bad = assess(session_rows(), evidence=evidence_for(session_rows(), sha256_verified=False))
    assert bad.verdict_code == ifs.EVIDENCE_HASH_UNVERIFIED
    with pytest.raises(InputError):
        admit_bars(bad, attempt(), SCHEDULE)
    good = assess()
    forged = dataclasses.replace(good, verdict_code=None, day_hash=good.day_hash, volume_sum=1)
    with pytest.raises(InputError):
        verify_day_integrity(forged)
    with pytest.raises(InputError):
        admit_bars(forged, attempt(), SCHEDULE)


def test_R33_day_integrity_fails_all_attempts_alike():
    rows = session_rows(t1130=CROSS_ALL_BUY)
    attempts = [attempt("a1"), attempt("a2", side=Side.SELL, limit="400.50", at_time="11:00"),
                attempt("a3", at_time="09:30")]
    results = run(rows, attempts, bhavcopy=bhavcopy_for(rows, total=1))  # ratio far above 1.02
    assert [code_of(r) for r in results] == [ifs.VOLUME_CHECK_FAILED] * 3
    assert all(s.filled_quantity == 0 for r in results for s in r.scenarios)
    assert len({r.day_integrity_hash for r in results}) == 1


# ---- R-5 / R-6 / R-7 / R-8 labels, bars, uniqueness, capped and empty --------------------


@pytest.mark.parametrize(
    "bad_label",
    ["2026-10-06 10:00:30", "2026-10-06 10:00", "2026-10-05 10:00:00", "06-10-2026 10:00:00", "2026-10-06T10:00:00",
     "2026-10-06 25:00:00", "2026-10-06 10:61:00", "", " 2026-10-06 10:00:00"],
)
def test_R5_label_malformed(bad_label):
    rows = session_rows()
    rows[100] = dataclasses.replace(rows[100], label=bad_label)
    (result,) = simulate_intraday(assess(rows), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.LABEL_MALFORMED


@pytest.mark.parametrize(
    "changes",
    [
        {"low": "400.30"},  # low above open and close
        {"high": "399.90"},  # high below open and close
        {"low": "0"}, {"low": "-1"},
        {"vol": -1},
        {"low": "359.00"},  # below the fixed band
        {"high": "441.00", "close": "400.00"},  # above the fixed band
    ],
)
def test_R6_bar_inconsistent(changes):
    rows = session_rows(t1130=changes)
    (result,) = simulate_intraday(assess(rows, bhavcopy=None), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.BAR_INCONSISTENT


def test_R6_volume_must_be_an_int_at_the_boundary():
    with pytest.raises(InputError):
        MinuteBar(label("10:00"), D(1), D(1), D(1), D(1), 1.5)  # type: ignore[arg-type]
    with pytest.raises(InputError):
        MinuteBar(label("10:00"), 1.0, D(1), D(1), D(1), 1)  # type: ignore[arg-type]


def test_R7_duplicate_bar():
    rows = session_rows()
    rows.insert(101, rows[100])
    (result,) = simulate_intraday(assess(rows), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.DUPLICATE_BAR


def test_R7_duplicate_outside_the_admitted_window_still_counts():
    rows = session_rows()
    rows.insert(1, rows[0])  # 09:15 twice, long before submission
    (result,) = simulate_intraday(assess(rows), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.DUPLICATE_BAR


def test_R7_bar_order():
    rows = session_rows()
    rows[100], rows[101] = rows[101], rows[100]
    (result,) = simulate_intraday(assess(rows), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.BAR_ORDER


def test_R8_capped_answer():
    rows = [row("10:00")] * 1000
    (result,) = simulate_intraday(assess(rows, bhavcopy=None), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.CAPPED_ANSWER
    nine_ninety_nine = session_rows()  # 375 rows is fine
    assert assess(nine_ninety_nine).passed


@pytest.mark.parametrize("kw", [{"bars": (), "empty_flag": False}, {"empty_flag": True}])
def test_R8_empty_answer(kw):
    rows = session_rows()
    evidence = evidence_for(rows, **kw)
    (result,) = simulate_intraday(assess(rows, evidence=evidence, bhavcopy=None), [attempt()], SCHEDULE)
    assert code_of(result) == ifs.EMPTY_ANSWER
    assert result.scenarios[0].outcome is IntradayOutcome.INCONCLUSIVE
    assert result.scenarios[0].outcome is not IntradayOutcome.MISSED


# ---- R-9 session and CAS class -----------------------------------------------------------


@pytest.mark.parametrize("session", [None, SessionFacts(False, "nse-closing-auction-2026-08-03")])
def test_R9_session_not_standard(session):
    (result,) = run(session=session)
    assert code_of(result) == ifs.SESSION_NOT_STANDARD


def test_R9_session_timing_version_changed():
    (result,) = run(session=SessionFacts(True, "nse-closing-auction-2027-01-01"))
    assert code_of(result) == ifs.SESSION_NOT_STANDARD


class _NoCas:
    pass


def test_R9_session_scope_unknown():
    (missing,) = run(cas=None, **{}) if False else simulate_intraday(
        assess_day(
            evidence_for(session_rows()), schedule=SCHEDULE, isin=ISIN, series=SERIES, stock_code=CODE,
            session_date=SESSION, tick=TICK, band=FIXED_BAND, session=SESSION_OK, cas_row=None,
            bhavcopy=bhavcopy_for(session_rows()),
        ),
        [attempt()], SCHEDULE,
    )
    assert code_of(missing) == ifs.SESSION_SCOPE_UNKNOWN
    (other_isin,) = run(cas=cas_row(isin="INE0TEST0002"))
    assert code_of(other_isin) == ifs.SESSION_SCOPE_UNKNOWN
    (other_day,) = run(cas=cas_row(session=NEXT_SESSION))
    assert code_of(other_day) == ifs.SESSION_SCOPE_UNKNOWN


def test_R9_verdict_records_the_class_table_version_and_source():
    day = assess(cas=cas_row("non_cas"))
    assert (day.cas_class, day.cas_table_version) == ("non_cas", "cas-test-v1")
    assert day.cas_source_ref == f"PUB-TEST-1:{sha256_hex('saved-file')}"
    assert day.continuous_end == "15:30"
    assert assess(cas=cas_row("cas")).continuous_end == "15:15"


def test_R11_lifetime_same_for_cas_and_non_cas():
    rows = session_rows(t1520=CROSS_ALL_BUY)
    cas, non_cas = (run(rows, cas=cas_row(c))[0] for c in ("cas", "non_cas"))
    assert cas.lifetime_end == non_cas.lifetime_end == at("15:15")
    assert (cas.continuous_end, non_cas.continuous_end) == ("15:15", "15:30")
    assert by_k(cas)[1].reason_code == by_k(non_cas)[1].reason_code == MISSED_THRESHOLD


# ---- R-10 to R-14 admission window -------------------------------------------------------


def test_R10_admission_start_1002_30():
    """Submission 10:02:30 with bars labelled 10:02 and 10:03: both excluded, 10:04 is the first admitted."""
    rows = session_rows(t1002=CROSS_ALL_BUY, t1003=CROSS_ALL_BUY)
    day = assess(rows)
    admitted = admit_bars(day, attempt(), SCHEDULE)
    assert admitted.bars[0].label == label("10:04") and admitted.window_first == "10:04"
    (result,) = simulate_intraday(day, [attempt()], SCHEDULE)
    assert by_k(result)[1].reason_code == MISSED_THRESHOLD
    before = {e.reason: e for e in result.exclusions}["before_submission"]
    assert (before.first_label, before.last_label) == (label("09:15"), label("10:03"))
    # Had the 10:04 bar crossed, it fills.
    (filled,) = run(session_rows(t1003=CROSS_ALL_BUY, t1004=CROSS_ALL_BUY))
    assert by_k(filled)[1].outcome is IntradayOutcome.FILLED


def test_R10_submission_on_a_whole_minute_admits_the_next_label():
    day = assess(session_rows())
    assert admit_bars(day, attempt(at_time="10:02", seconds=0), SCHEDULE).window_first == "10:03"
    assert admit_bars(day, attempt(at_time="10:02", seconds=1), SCHEDULE).window_first == "10:04"


def test_R10_a_crossing_bar_before_submission_gives_no_fill():
    (result,) = run(session_rows(t1003=CROSS_ALL_BUY))
    assert all(s.filled_quantity == 0 and s.reason_code == MISSED_THRESHOLD for s in result.scenarios)


def test_R11_lifetime_end_1514_admitted_1515_excluded():
    (late,) = run(session_rows(t1515=CROSS_ALL_BUY))
    assert by_k(late)[1].reason_code == MISSED_THRESHOLD
    assert {e.reason: e.first_label for e in late.exclusions}["after_lifetime"] == label("15:15")
    (inside,) = run(session_rows(t1514=CROSS_ALL_BUY))
    assert by_k(inside)[1].outcome is IntradayOutcome.FILLED
    admitted = admit_bars(assess(session_rows()), attempt(), SCHEDULE)
    assert admitted.bars[-1].label == label("15:14") and admitted.window_last == "15:14"


def test_R11_bars_in_the_closing_auction_window_are_never_credited():
    rows = session_rows(t1520=CROSS_ALL_BUY, t1530=CROSS_ALL_BUY, t1535=CROSS_ALL_BUY)
    (result,) = run(rows)
    assert all(s.reason_code == MISSED_THRESHOLD for s in result.scenarios)


def test_R12_no_admissible_bar():
    (result,) = run(attempts=[attempt(at_time="15:13", seconds=30)])
    assert code_of(result) == ifs.NO_ADMISSIBLE_BAR
    assert result.scenarios[0].outcome is IntradayOutcome.INCONCLUSIVE
    (edge,) = run(attempts=[attempt(at_time="15:13", seconds=0)])  # first label 15:14 is admissible
    assert code_of(edge) is None


def test_R13_exclusion_accounting():
    rows = session_rows()
    (result,) = run(rows)
    reasons = {e.reason: e for e in result.exclusions}
    assert reasons["before_submission"].count == 49  # labels 09:15 to 10:03
    assert reasons["after_lifetime"].count == 15 and reasons["after_lifetime"].last_label == label("15:29")
    assert "outside_session" not in reasons
    extra = rows + [row("15:36"), row("15:40")]
    (more,) = run(extra, bhavcopy=bhavcopy_for(rows))
    assert {e.reason: e.count for e in more.exclusions}["outside_session"] == 2


def test_R14_gap_in_sequence():
    (result,) = run(session_rows(t1200=None))
    assert code_of(result) == ifs.GAP_IN_SEQUENCE


def test_R14_gap_leading_minute():
    (result,) = run(session_rows(t1004=None), bhavcopy="auto")
    assert code_of(result) == ifs.GAP_IN_SEQUENCE
    (outside,) = run(session_rows(t1003=None))  # before the window: not a gap for this attempt
    assert code_of(outside) is None


def test_R14_a_gap_after_the_lifetime_is_not_a_gap():
    (result,) = run(session_rows(t1520=None))
    assert code_of(result) is None


# ---- R-15 / R-16 / R-17 whole-day volume and price gates ---------------------------------


@pytest.mark.parametrize("cas_class", ["cas", "non_cas"])
def test_R15_volume_ratio_above_upper_1_02(cas_class):
    over = total_rows(1_021_000)
    (result,) = run(over, cas=cas_row(cas_class), bhavcopy=bhavcopy_for(over, total=1_000_000))
    assert code_of(result) == ifs.VOLUME_CHECK_FAILED
    assert result.volume_ratio == D("1.021")
    exact = total_rows(1_020_000)
    (at_bound,) = run(exact, cas=cas_row(cas_class), bhavcopy=bhavcopy_for(exact, total=1_000_000))
    assert code_of(at_bound) is None and at_bound.volume_ratio == D("1.02")


def test_R15_volume_lower_check_skipped_while_provisional():
    low = total_rows(800_000)
    (result,) = run(low, bhavcopy=bhavcopy_for(low, total=1_000_000))
    assert code_of(result) is None
    assert result.volume_ratio == D("0.8") and result.volume_lower_check == "skipped_provisional"
    assert result.lower_bound_status == "provisional"


def test_R15_volume_lower_bound_provisional_flag_in_hash():
    (result,) = run()
    assert result.lower_bound_status == "provisional"
    flipped = dataclasses.replace(result, lower_bound_status="final")
    assert seal(flipped, "result_hash").result_hash != result.result_hash
    final = final_schedule()
    (other,) = run(attempts=[attempt(schedule=final)], schedule=final)
    assert other.result_hash != result.result_hash and other.lower_bound_status == "final"


def test_R15_volume_below_final_lower_bound_fails():
    sched = final_schedule()
    for total, expected in [(890_000, ifs.VOLUME_CHECK_FAILED), (900_000, None)]:
        rows = total_rows(total)
        (result,) = run(rows, [attempt(schedule=sched)], schedule=sched, bhavcopy=bhavcopy_for(rows, total=1_000_000))
        assert code_of(result) == expected, total
        if expected is None:
            assert result.volume_lower_check == "passed"


def test_R15_volume_ratio_40pct_final_lower_fails():
    sched = final_schedule()
    rows = total_rows(400_000)
    (result,) = run(rows, [attempt(schedule=sched)], schedule=sched, bhavcopy=bhavcopy_for(rows, total=1_000_000))
    assert code_of(result) == ifs.VOLUME_CHECK_FAILED and result.volume_ratio == D("0.4")
    # Under version 1 the same 40 percent day passes with the lower check skipped.
    (provisional,) = run(rows, bhavcopy=bhavcopy_for(rows, total=1_000_000))
    assert code_of(provisional) is None


def test_R15_volume_band_per_cas_class():
    sched = final_schedule(cas_lower="0.50", non_cas_lower="0.90")
    rows = total_rows(700_000)
    kw = dict(schedule=sched, bhavcopy=bhavcopy_for(rows, total=1_000_000))
    (cas,) = run(rows, [attempt(schedule=sched)], cas=cas_row("cas"), **kw)
    (non_cas,) = run(rows, [attempt(schedule=sched)], cas=cas_row("non_cas"), **kw)
    assert code_of(cas) is None and code_of(non_cas) == ifs.VOLUME_CHECK_FAILED


def test_R15_volume_sum_covers_excluded_bars():
    """A bar before the submission, and one after the lifetime, both count toward the day sum."""
    base = session_rows()
    ttq = sum(r.volume for r in base)
    for overrides in ({"t0920": {"vol": 100_000}}, {"t1520": {"vol": 100_000}}, {"t1535": {"vol": 100_000}}):
        rows = session_rows(**overrides) + ([row("15:35", vol=100_000)] if "t1535" in overrides else [])
        rows = [r for r in rows]
        (result,) = run(rows, bhavcopy=bhavcopy_for(base, total=ttq))
        assert code_of(result) == ifs.VOLUME_CHECK_FAILED, overrides


def test_R15_volume_window_is_0915_to_1535_inclusive():
    base = session_rows()
    ttq = sum(r.volume for r in base)
    inside = base + [row("15:35", vol=100_000)]
    (counted,) = run(inside, bhavcopy=bhavcopy_for(base, total=ttq))
    assert code_of(counted) == ifs.VOLUME_CHECK_FAILED
    outside = base + [row("15:36", vol=100_000)]
    (ignored,) = run(outside, bhavcopy=bhavcopy_for(base, total=ttq))
    assert code_of(ignored) is None


def test_R16_volume_reference_missing():
    for reference in (None, dataclasses.replace(bhavcopy_for(session_rows()), series="BE"),
                      dataclasses.replace(bhavcopy_for(session_rows()), session_date=NEXT_SESSION),
                      dataclasses.replace(bhavcopy_for(session_rows()), isin="INE0TEST0002"),
                      dataclasses.replace(bhavcopy_for(session_rows()), total_traded_quantity=0)):
        (result,) = run(bhavcopy=reference)
        assert code_of(result) == ifs.VOLUME_REFERENCE_MISSING


def test_R16_price_range_below_day_low():
    rows = session_rows(t1130={"low": "399.40", "vol": 1000})
    ref = bhavcopy_for(rows)
    tight = dataclasses.replace(ref, low=D("399.50"))  # the bar low is 0.10 under the day low, two ticks
    (result,) = run(rows, bhavcopy=tight)
    assert code_of(result) == ifs.PRICE_RANGE_CHECK_FAILED
    within = dataclasses.replace(ref, low=D("399.45"))  # exactly one tick under
    (ok,) = run(rows, bhavcopy=within)
    assert code_of(ok) is None


def test_R16_price_range_above_day_high():
    rows = session_rows(t1130={"high": "400.60", "vol": 1000})
    ref = dataclasses.replace(bhavcopy_for(rows), high=D("400.50"))
    (result,) = run(rows, bhavcopy=ref)
    assert code_of(result) == ifs.PRICE_RANGE_CHECK_FAILED
    (ok,) = run(rows, bhavcopy=dataclasses.replace(ref, high=D("400.55")))
    assert code_of(ok) is None


def test_R16_price_range_excluded_bar_extreme():
    """A bar the attempt may not use (before submission) still fails the whole day."""
    rows = session_rows(t0920={"low": "395.00", "vol": 1000})
    ref = dataclasses.replace(bhavcopy_for(rows), low=D("399.80"))
    (result,) = run(rows, [attempt(at_time="11:00")], bhavcopy=ref)
    assert code_of(result) == ifs.PRICE_RANGE_CHECK_FAILED


def test_R17_daily_volume_record_only():
    rows = session_rows(t1130=CROSS_ALL_BUY)
    (agree,) = run(rows, daily=sum(r.volume for r in rows))
    (disagree,) = run(rows, daily=10_000)
    (unchecked,) = run(rows)
    day_agree = assess(rows, daily=sum(r.volume for r in rows))
    day_disagree = assess(rows, daily=10_000)
    assert (day_agree.daily_bar_volume_disagree, day_disagree.daily_bar_volume_disagree) == (False, True)
    assert assess(rows).daily_bar_volume_disagree is None
    # Never changes an outcome.
    assert [by_k(r)[1].outcome for r in (agree, disagree, unchecked)] == [IntradayOutcome.FILLED] * 3


# ---- R-18 / R-19 freshness and hash ------------------------------------------------------


def test_R18_fetched_before_close():
    for moment in (at("15:39", 59), at("14:00"), at("09:00") - timedelta(days=1)):
        (result,) = run(fetched_at=moment)
        assert code_of(result) == ifs.EVIDENCE_FETCHED_BEFORE_CLOSE, moment
    (ok,) = run(fetched_at=at("15:40"))
    assert code_of(ok) is None and ok.evidence_fetch_rule == "same_day"


def test_R18_fetched_too_late():
    (late,) = run(fetched_at=at("23:30", 1))
    assert code_of(late) == ifs.EVIDENCE_FETCHED_TOO_LATE
    (edge,) = run(fetched_at=at("23:30"))
    assert code_of(edge) is None


def test_R18_fetched_next_day_default_off():
    (result,) = run(fetched_at=datetime.combine(NEXT_SESSION, time(8, 30), tzinfo=IST))
    assert code_of(result) == ifs.EVIDENCE_FETCHED_TOO_LATE


def test_R18_next_day_fetch_refused_when_unset():
    assert SCHEDULE.next_day_fetch is None
    rows = session_rows(t1130=CROSS_ALL_BUY)
    for moment in (
        datetime.combine(NEXT_SESSION, time(8, 30), tzinfo=IST),
        datetime.combine(NEXT_SESSION, time(9, 0), tzinfo=IST),
        at("23:45"),
    ):
        (result,) = run(rows, fetched_at=moment)
        assert code_of(result) == ifs.EVIDENCE_FETCHED_TOO_LATE, moment
        assert all(s.filled_quantity == 0 for s in result.scenarios)



def test_R18_next_day_fetch_window_pinned():
    sched = schedule_with(next_day_fetch=NEXT_DAY)
    assert sched.schedule_hash != SCHEDULE.schedule_hash
    rows = session_rows(t1130=CROSS_ALL_BUY)
    nd = lambda h, m, s=0: datetime.combine(NEXT_SESSION, time(h, m, s), tzinfo=IST)  # noqa: E731
    (ok,) = run(rows, [attempt(schedule=sched)], schedule=sched, fetched_at=nd(8, 30))
    assert code_of(ok) is None and ok.evidence_fetch_rule == "next_day_exception_v1"
    assert by_k(ok)[1].outcome is IntradayOutcome.FILLED
    for moment in (nd(7, 59, 59), nd(9, 25, 1), nd(9, 30), datetime.combine(date(2026, 10, 8), time(8, 30), tzinfo=IST)):
        (result,) = run(rows, [attempt(schedule=sched)], schedule=sched, fetched_at=moment)
        assert code_of(result) in (ifs.EVIDENCE_FETCHED_TOO_LATE,), moment
    (edge,) = run(rows, [attempt(schedule=sched)], schedule=sched, fetched_at=nd(9, 25))
    assert code_of(edge) is None
    # The same-day rule is still labelled same_day under the new version.
    (same_day,) = run(rows, [attempt(schedule=sched)], schedule=sched)
    assert same_day.evidence_fetch_rule == "same_day"
    # An evidence record with no next session date cannot use the exception.
    (no_date,) = run(rows, [attempt(schedule=sched)], schedule=sched, fetched_at=nd(8, 30), next_session_date=None)
    assert code_of(no_date) == ifs.EVIDENCE_FETCHED_TOO_LATE


def test_R19_hash_unverified():
    (result,) = run(sha256_verified=False)
    assert code_of(result) == ifs.EVIDENCE_HASH_UNVERIFIED


def test_R18_no_evidence_fetched_reasons():
    for reason in ifs.NO_EVIDENCE_REASONS:
        (result,) = no_evidence_results([attempt()], SCHEDULE, reason=reason)
        assert code_of(result) == ifs.NO_EVIDENCE_FETCHED and result.inconclusive_detail == reason
        assert result.source_sha256 is None
    with pytest.raises(InputError):
        no_evidence_results([attempt()], SCHEDULE, reason="because")


# ---- R-20 to R-22, R-32 screens, thresholds, locked and single-price bars ----------------


def test_R20_tick_band_codes():
    crossing = session_rows(t1130=CROSS_ALL_BUY)
    (off_tick,) = run(crossing, [attempt(limit="399.52")])
    assert {s.reason_code for s in off_tick.scenarios} == {REJECTED_OFF_TICK}
    assert off_tick.scenarios[0].outcome is IntradayOutcome.REJECTED
    (outside,) = run(crossing, [attempt(limit="445.00")])
    assert {s.reason_code for s in outside.scenarios} == {REJECTED_OUTSIDE_BAND}
    (unavailable,) = run(crossing, [attempt(band=BandUnavailable("no file"))], band=BandUnavailable("no file"))
    assert {s.reason_code for s in unavailable.scenarios} == {NO_FILL_BAND_UNAVAILABLE}
    stale = PriceBand("fixed", D("360.00"), D("440.00"), date(2026, 10, 5), "old", sha256_hex("old"))
    (mismatch,) = run(crossing, [attempt(band=stale)], band=stale)
    assert {s.reason_code for s in mismatch.scenarios} == {NO_FILL_BAND_DATE_MISMATCH}
    assert mismatch.scenarios[0].outcome is IntradayOutcome.NO_ASSUMED_FILL


def test_R20_tick_off_grid_wins_over_the_band_verdicts():
    (result,) = run(attempts=[attempt(limit="445.03")])
    assert result.scenarios[0].reason_code == REJECTED_OFF_TICK


def test_R21_thresholds_k123():
    # Buy limit 399.50: thresholds 399.45, 399.40, 399.35.
    cases = [("399.45", (1,)), ("399.40", (1, 2)), ("399.35", (1, 2, 3)), ("399.46", ())]
    for low, filled_ks in cases:
        (result,) = run(session_rows(t1130={"low": low, "open_": "399.60", "close": "399.60", "high": "399.70",
                                            "vol": 100_000}))
        got = tuple(k for k, s in by_k(result).items() if s.filled_quantity > 0)
        assert got == filled_ks, low
    # Sell limit 400.50: thresholds 400.55, 400.60, 400.65.
    for high, filled_ks in [("400.55", (1,)), ("400.60", (1, 2)), ("400.65", (1, 2, 3)), ("400.54", ())]:
        (result,) = run(
            session_rows(t1130={"high": high, "open_": "400.40", "close": "400.40", "low": "400.30", "vol": 100_000}),
            [attempt(side=Side.SELL, limit="400.50")],
        )
        got = tuple(k for k, s in by_k(result).items() if s.filled_quantity > 0)
        assert got == filled_ks, high


def test_R21_fill_price_is_limit():
    odd = {"low": "399.10", "open_": "399.15", "close": "399.12", "high": "399.20", "vol": 100_000}
    (result,) = run(session_rows(t1130=odd))
    for scenario in result.scenarios:
        assert scenario.fill_price == D("399.50")
        assert scenario.fill_price not in (D("399.10"), D("399.15"), D("399.12"), D("399.20"))


def test_invariant_perturb_open_close():
    """I-2: open and close, moved anywhere inside [low, high], change no fill."""
    rows = session_rows(t1130=CROSS_ALL_BUY)
    base = run(rows)[0]
    moved = [dataclasses.replace(r, open=r.low, close=r.high) for r in rows]
    other = run(moved)[0]
    assert [(s.outcome, s.reason_code, s.filled_quantity, s.fill_price) for s in other.scenarios] == [
        (s.outcome, s.reason_code, s.filled_quantity, s.fill_price) for s in base.scenarios
    ]


def test_R22_locked_bar_not_credited():
    locked = {"low": "440.00", "high": "440.00", "open_": "440.00", "close": "440.00", "vol": 100_000}
    rows = session_rows(NEAR_UPPER, t1130=locked, t1131=locked)
    ref = bhavcopy_for(rows)
    (result,) = run(rows, [attempt(limit="440.00")], bhavcopy=ref)
    assert result.locked_bars == 2
    assert {s.reason_code for s in result.scenarios} == {MISSED_LOCKED_AT_BAND}
    assert all(s.crossing_bars == 0 and s.volume_cap == 0 for s in result.scenarios)


def test_R22_locked_buy_at_upper_gives_missed_locked():
    rows = [row(h, low="440.00", high="440.00", open_="440.00", close="440.00") for h in hhmm_range()]
    (result,) = run(rows, [attempt(limit="440.00")])
    assert {s.reason_code for s in result.scenarios} == {MISSED_LOCKED_AT_BAND}
    assert {s.outcome for s in result.scenarios} == {IntradayOutcome.MISSED}


def test_R22_locked_sell_at_lower_gives_missed_locked():
    rows = [row(h, low="360.00", high="360.00", open_="360.00", close="360.00") for h in hhmm_range()]
    (result,) = run(rows, [attempt(side=Side.SELL, limit="360.00")])
    assert {s.reason_code for s in result.scenarios} == {MISSED_LOCKED_AT_BAND}


def test_R22_locked_check_precedes_threshold():
    """A locked bar is never tested for crossing: its volume adds no allowance next to a real crossing bar."""
    locked = {"low": "440.00", "high": "440.00", "open_": "440.00", "close": "440.00", "vol": 900_000}
    cross = {"low": "439.90", "high": "440.00", "open_": "440.00", "close": "440.00", "vol": 5000}
    rows = session_rows(NEAR_UPPER, t1130=locked, t1140=cross)
    (result,) = run(rows, [attempt(limit="440.00", quantity=1000)], bhavcopy=bhavcopy_for(rows))
    k1 = by_k(result)[1]
    assert (k1.crossing_bars, k1.crossing_volume, k1.volume_cap) == (1, 5000, 50)
    assert result.locked_bars == 1


def test_R22_single_price_at_other_edge_not_locked():
    low_edge = {"low": "360.00", "high": "360.00", "open_": "360.00", "close": "360.00", "vol": 100_000}
    (buy,) = run(session_rows(t1130=low_edge), [attempt(limit="360.20")])
    assert buy.locked_bars == 0 and by_k(buy)[1].outcome is IntradayOutcome.FILLED
    high_edge = {"low": "440.00", "high": "440.00", "open_": "440.00", "close": "440.00", "vol": 100_000}
    (sell,) = run(session_rows(t1130=high_edge), [attempt(side=Side.SELL, limit="439.80")])
    assert sell.locked_bars == 0 and by_k(sell)[1].outcome is IntradayOutcome.FILLED


def _flat_rows(price: str):
    return [row(h, low=price, high=price, open_=price, close=price) for h in hhmm_range()]


def test_R32_no_band_single_price_not_credited():
    rows = _flat_rows("399.30")  # would cross a 399.50 buy if it were credited
    (result,) = run(rows, band=NO_BAND, attempts=[attempt(band=NO_BAND)])
    assert result.ambiguous_single_price_bars > 0
    assert {s.reason_code for s in result.scenarios} == {NO_FILL_AMBIGUOUS_SINGLE_PRICE}
    assert {s.outcome for s in result.scenarios} == {IntradayOutcome.NO_ASSUMED_FILL}


def test_R32_no_band_single_price_precedes_threshold():
    for price in ("399.30", "400.50"):  # at the threshold and short of it
        (result,) = run(_flat_rows(price), band=NO_BAND, attempts=[attempt(band=NO_BAND)])
        assert {s.reason_code for s in result.scenarios} == {NO_FILL_AMBIGUOUS_SINGLE_PRICE}, price


def test_R32_no_band_single_price_mixed_partial():
    rows = session_rows(
        t1130={"low": "399.30", "high": "399.30", "open_": "399.30", "close": "399.30", "vol": 900_000},
        t1140={"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000},
    )
    (result,) = run(rows, band=NO_BAND, attempts=[attempt(band=NO_BAND, quantity=1000)], bhavcopy=bhavcopy_for(rows))
    k1 = by_k(result)[1]
    assert (k1.outcome, k1.reason_code, k1.filled_quantity, k1.crossing_volume) == (
        IntradayOutcome.PARTIAL, PARTIAL_VOLUME_CAP, 50, 5000,
    )
    assert result.ambiguous_single_price_bars == 1


def _daily_reason(bar_price: str, side: Side, limit: str, band: PriceBand) -> str:
    daily_tick = TICK
    bar = SessionBar(ISIN, "NSE", SESSION, D(bar_price), D(bar_price), D(bar_price), D(bar_price), 20000, "raw", band,
                     "test-bar")
    order = LimitOrder("o1", ISIN, "NSE", side, 50, D(limit), D(limit), SESSION,
                       datetime.fromisoformat("2026-10-05T18:00:00+05:30"), date(2026, 10, 5), daily_tick)
    return simulate_session([order], bar, load_fill_scenarios().get("base"))[0].reason_code


def _assert_daily_parity_locked(side, limit, price):
    (result,) = run(_flat_rows(price), [attempt(side=side, limit=limit)])
    assert by_k(result)[1].reason_code == _daily_reason(price, side, limit, FIXED_BAND) == MISSED_LOCKED_AT_BAND


def test_R22_daily_parity_locked_bar_buy():
    _assert_daily_parity_locked(Side.BUY, "440.00", "440.00")


def test_R22_daily_parity_locked_bar_sell():
    _assert_daily_parity_locked(Side.SELL, "360.00", "360.00")


@pytest.mark.parametrize("price", ["399.45", "400.50"])  # at the base threshold and short of it
def test_R32_daily_parity_no_band_single_price(price):
    (result,) = run(_flat_rows(price), band=NO_BAND, attempts=[attempt(band=NO_BAND)])
    assert by_k(result)[1].reason_code == _daily_reason(price, Side.BUY, "399.50", NO_BAND)
    assert by_k(result)[1].reason_code == NO_FILL_AMBIGUOUS_SINGLE_PRICE


# ---- R-23 to R-26 participation, FIFO, siblings ------------------------------------------


def test_R23_participation_crossing_volume():
    """The cap comes from crossing post-submission bars only, never from the whole session."""
    rows = session_rows(t1130={"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000})
    (result,) = run(rows, [attempt(quantity=1000)])
    k = by_k(result)
    assert (k[1].volume_cap, k[2].volume_cap, k[3].volume_cap) == (50, 25, 12)  # floor(12.5)
    assert k[1].crossing_volume == 5000 and k[1].crossing_bars == 1
    assert (k[1].outcome, k[1].reason_code, k[1].filled_quantity) == (IntradayOutcome.PARTIAL, PARTIAL_VOLUME_CAP, 50)
    full = run(rows, [attempt(quantity=20)])[0]
    assert by_k(full)[1].outcome is IntradayOutcome.FILLED and by_k(full)[1].reason_code == FILLED_AT_LIMIT


def test_R23_participation_zero_cap():
    rows = session_rows(t1130={"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 30})
    (result,) = run(rows, [attempt(quantity=10)])
    assert by_k(result)[1].reason_code == MISSED_VOLUME_CAP_ZERO  # floor(0.3)
    assert by_k(result)[1].outcome is IntradayOutcome.MISSED


def test_R23_participation_ignores_bars_before_submission():
    big_before = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 900_000}
    small_after = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 1000}
    rows = session_rows(t0930=big_before, t1130=small_after)
    (result,) = run(rows, [attempt(quantity=1000)], bhavcopy=bhavcopy_for(rows))
    assert by_k(result)[1].volume_cap == 10  # only the 11:30 bar


def test_R24_fifo_shared_allowance():
    cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000}  # allowance 50 at k=1
    rows = session_rows(t1130=cross)
    a = attempt("a", quantity=30, at_time="10:00")
    b = attempt("b", quantity=30, at_time="10:01")
    ra, rb = run(rows, [a, b])
    assert (by_k(ra)[1].filled_quantity, by_k(rb)[1].filled_quantity) == (30, 20)
    assert by_k(rb)[1].outcome is IntradayOutcome.PARTIAL
    assert by_k(rb)[1].volume_cap == 20


def test_R24_fifo_order_independent():
    cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000}
    rows = session_rows(t1130=cross)
    attempts = [attempt("c", quantity=30, at_time="10:02"), attempt("a", quantity=30, at_time="10:00"),
                attempt("b", quantity=30, at_time="10:00")]
    forward = run(rows, attempts)
    backward = run(rows, list(reversed(attempts)))
    assert [r.intent_id for r in forward] == ["a", "b", "c"]  # (submission time, intent id)
    assert [r.result_hash for r in forward] == [r.result_hash for r in backward]
    assert [by_k(r)[1].filled_quantity for r in forward] == [30, 20, 0]


def test_R24_fifo_spans_both_sides():
    cross_buy = {"low": "399.30", "high": "400.80", "open_": "400.00", "close": "400.00", "vol": 5000}
    rows = session_rows(t1130=cross_buy)  # crosses a 399.50 buy and a 400.50 sell
    buy = attempt("buy", quantity=30, at_time="10:00")
    sell = attempt("sell", side=Side.SELL, limit="400.50", quantity=30, at_time="10:01")
    r_buy, r_sell = run(rows, [buy, sell])
    assert (by_k(r_buy)[1].filled_quantity, by_k(r_sell)[1].filled_quantity) == (30, 20)


def test_R24_fifo_demand_charge_latest_first():
    """An earlier partial fill still charges its full quantity, from the latest credited label back."""
    cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000}
    rows = session_rows(t1130=cross, t1200=cross)  # allowance 50 on each, k=1
    a = attempt("a", quantity=70, at_time="10:00")
    b = attempt("b", quantity=100, at_time="10:01")
    ra, rb = run(rows, [a, b])
    assert by_k(ra)[1].filled_quantity == 70
    assert by_k(ra)[1].allowance_charged == D("70")  # 50 from 12:00, then 20 from 11:30
    assert by_k(rb)[1].volume_cap == 30 and by_k(rb)[1].filled_quantity == 30
    # An earlier attempt that can only fill part still charges all it asked for.
    big = attempt("big", quantity=500, at_time="10:00")
    follower = attempt("f", quantity=10, at_time="10:01")
    rbig, rf = run(rows, [big, follower])
    assert by_k(rbig)[1].filled_quantity == 100 and by_k(rbig)[1].allowance_charged == D("100")
    assert by_k(rf)[1].filled_quantity == 0 and by_k(rf)[1].reason_code == MISSED_VOLUME_CAP_ZERO


def test_R24_fifo_rejected_attempt_charges_nothing():
    cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000}
    rows = session_rows(t1130=cross)
    rejected = attempt("a", quantity=500, limit="399.52", at_time="10:00")
    later = attempt("b", quantity=30, at_time="10:01")
    ra, rb = run(rows, [rejected, later])
    assert by_k(ra)[1].reason_code == REJECTED_OFF_TICK and by_k(ra)[1].allowance_charged == 0
    assert by_k(rb)[1].filled_quantity == 30 and by_k(rb)[1].volume_cap == 50


def _fractional_book():
    """A quantity 100; outside allowance 0.2 (bar before B's submission, A only); shared bar 0.9; B-only bar 1.1."""
    sched = schedule_with(scenarios=[{"id": "only", "k_ticks": 1, "volume_participation": "0.001"}])
    a_cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60"}
    b_only = {"low": "399.60", "high": "399.80", "open_": "399.70", "close": "399.70"}  # crosses 399.70 not 399.50

    def build(outside: bool):
        rows = session_rows(
            t1000={**a_cross, "vol": 200} if outside else {},
            t1130={**a_cross, "vol": 900},
            t1200={**b_only, "vol": 1100},
        )
        a = attempt("a", quantity=100, at_time="09:30", seconds=0, schedule=sched)
        b = attempt("b", quantity=5, limit="399.70", at_time="11:00", seconds=0, schedule=sched)
        return run(rows, [a, b], schedule=sched)

    return build


def test_R24_fifo_codex_r2_counterexample():
    # Integer book from the Codex r2 review: A buys at 10:05 (crosses 10:30 and 11:30), B buys at 11:00 (11:30).
    cross = {"low": "399.30", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 5000}

    def book(with_early: bool, scale: int = 1, a_qty: int = 60):
        rows = session_rows(
            t1030={**cross, "vol": 5000 * scale} if with_early else {},
            t1130={**cross, "vol": 8000},
        )
        a = attempt("a", quantity=a_qty, at_time="10:05", seconds=0)
        b = attempt("b", quantity=60, at_time="11:00", seconds=0)
        return run(rows, [a, b])

    def b_view(results):
        b = results[1]
        return [(s.k_ticks, s.outcome, s.reason_code, s.filled_quantity, s.volume_cap, s.allowance_charged, s.cap_label_window)
                for s in b.scenarios]

    with_early, without_early, scaled = book(True), book(False), book(True, scale=7)
    assert b_view(with_early) == b_view(without_early) == b_view(scaled)
    # A bigger first order does move A's own fill (the early bar adds allowance for A only), yet B is untouched.
    big_with, big_without = book(True, a_qty=100), book(False, a_qty=100)
    assert by_k(big_with[0])[1].filled_quantity == 100 and by_k(big_without[0])[1].filled_quantity == 80
    assert b_view(big_with) == b_view(big_without)
    # The fractional book: allowances after p_k are 0.2 outside, 0.9 shared, 1.1 B-only.
    build = _fractional_book()
    with_outside, without_outside = build(True), build(False)
    b1, b2 = (by_k(r[1])[1] for r in (with_outside, without_outside))
    assert b1.filled_quantity == b2.filled_quantity == 1
    assert (b1.volume_cap, b2.volume_cap) == (1, 1)
    # A fill-charge book would leave B at 2 here: with the outside bar removed, A fills floor(0.9) = 0 yet still
    # charges the 0.9 it was asking for.
    a_without = by_k(without_outside[0])[1]
    assert a_without.filled_quantity == 0 and a_without.allowance_charged == D("0.9") + D("0")
    assert by_k(with_outside[0])[1].allowance_charged == D("1.1") - D("0")


def test_R25_sibling_unresolved():
    rows = session_rows(t1130=CROSS_ALL_BUY, t1500=None)
    early = attempt("a", at_time="10:00")  # its window reaches 15:00, so the missing bar is a gap for it
    late = attempt("b", at_time="15:05")  # window 15:06 to 15:14, no gap, but its earlier sibling is unresolved
    ra, rb = run(rows, [early, late])
    assert code_of(ra) == ifs.GAP_IN_SEQUENCE
    assert code_of(rb) == ifs.SIBLING_UNRESOLVED
    assert all(s.filled_quantity == 0 for s in rb.scenarios)
    # Without the earlier sibling, the late attempt resolves on its own.
    (alone,) = run(rows, [late])
    assert code_of(alone) is None


def test_invariant_sibling_gap_only_inconclusive():
    """I-4d: a bar that leaves an earlier sibling gapped moves B only to sibling_unresolved."""
    cross = CROSS_ALL_BUY
    base_rows = session_rows(t1130=cross)
    a = attempt("a", at_time="10:00")
    b = attempt("b", at_time="11:00")
    reference = run(base_rows, [a, b])
    gapped = run(session_rows(t1030=None, t1130=cross), [a, b])  # 10:30 is inside A's window and before B's
    assert code_of(reference[1]) is None
    assert code_of(gapped[0]) == ifs.GAP_IN_SEQUENCE and code_of(gapped[1]) == ifs.SIBLING_UNRESOLVED


# ---- R-26 to R-31 ------------------------------------------------------------------------


def test_R26_k1_fill_k3_miss_flag():
    rows = session_rows(t1130={"low": "399.44", "high": "399.70", "open_": "399.60", "close": "399.60", "vol": 100_000})
    (result,) = run(rows)
    assert result.k1_fill_k3_miss is True
    assert [s.filled_quantity > 0 for s in result.scenarios] == [True, False, False]
    (all_three,) = run(session_rows(t1130={**CROSS_ALL_BUY, "vol": 100_000}))
    assert all_three.k1_fill_k3_miss is False
    (none,) = run(session_rows())
    assert none.k1_fill_k3_miss is False


def test_R26_k123_come_from_one_call_over_the_same_evidence():
    (result,) = run(session_rows(t1130={**CROSS_ALL_BUY, "vol": 100_000}))
    assert [s.scenario_id for s in result.scenarios] == ["base", "adverse", "pessimistic"]
    assert len({result.source_sha256}) == 1


def test_R27_label_semantics_unproven():
    assert SCHEDULE.label_semantics == "unproven"
    (result,) = run()
    assert result.label_semantics == "unproven" and code_of(result) is None
    # An "unproven" schedule never refuses on the label claim.
    assert not SCHEDULE.label_claim_refused()


HASH_A, HASH_B = sha256_hex("evidence-record"), sha256_hex("approval-record")


@pytest.mark.parametrize(
    "ref, approval",
    [
        ({"source_kind": "breeze_bar_snapshot", "record_hash": HASH_A}, HASH_B),  # the probe record, even approved
        ({"source_kind": "icici_written_statement", "record_hash": HASH_A}, None),
        (None, HASH_B),
        (None, None),
        ({"source_kind": "unknown_source", "record_hash": HASH_A}, HASH_B),
    ],
)
def test_R27_label_claim_refused(ref, approval):
    sched = schedule_with(label_semantics="proven", label_evidence_ref=ref, label_approval_ref=approval)
    (result,) = run(session_rows(t1130=CROSS_ALL_BUY), [attempt(schedule=sched)], schedule=sched)
    assert code_of(result) == ifs.LABEL_SEMANTICS_UNPROVEN
    assert all(s.filled_quantity == 0 for s in result.scenarios)


def test_R27_label_claim_with_probe_ref_only_refused():
    sched = schedule_with(
        label_semantics="proven",
        label_evidence_ref={"source_kind": "breeze_bar_snapshot", "record_hash": HASH_A},
        label_approval_ref=HASH_B,
    )
    assert sched.label_claim_refused()


def test_R27_label_claim_without_approval_ref_refused():
    sched = schedule_with(
        label_semantics="proven",
        label_evidence_ref={"source_kind": "icici_written_statement", "record_hash": HASH_A},
    )
    assert sched.label_claim_refused()


def test_R27_label_claim_without_evidence_ref_refused():
    assert schedule_with(label_semantics="proven", label_approval_ref=HASH_B).label_claim_refused()


def test_R27_label_claim_with_both_refs_changes_no_admission():
    """Relaxation is a later approved task: a fully backed claim still gets the conservative window."""
    sched = schedule_with(
        label_semantics="proven",
        label_evidence_ref={"source_kind": "icici_written_statement", "record_hash": HASH_A},
        label_approval_ref=HASH_B,
    )
    assert not sched.label_claim_refused()
    day = assess(schedule=sched)
    assert admit_bars(day, attempt(schedule=sched), sched).window_first == "10:04"


def test_R28_result_hash_stable():
    rows = session_rows(t1130=CROSS_ALL_BUY)
    first, second = run(rows)[0], run(rows)[0]
    assert first.result_hash == second.result_hash and len(first.result_hash) == 64
    other_source = run(rows, source_sha256=sha256_hex("another payload"))[0]
    assert other_source.result_hash != first.result_hash
    other_attempt = run(rows, [attempt(at_time="10:03")])[0]
    assert other_attempt.result_hash != first.result_hash
    assert first.schedule_hash == SCHEDULE.schedule_hash and first.source_sha256 is not None
    assert seal(first, "result_hash").result_hash == first.result_hash


def test_R29_labels_present():
    (result,) = run()
    assert result.labels == (
        "OHLCV assumption (OI-3)", "conservative simulated fill from evidence",
        "not evidence of actual fill probability",
    )
    assert SCHEDULE.assumption_note == "Simulation assumptions, not evidence of actual fill probability."


def test_R31_result_content():
    (result,) = run(session_rows(t1130={**CROSS_ALL_BUY, "vol": 100_000}), cas=cas_row("non_cas"))
    assert result.lifetime_end == at("15:15") and result.cas_class == "non_cas" and result.continuous_end == "15:30"
    assert result.interval == "1minute" and result.gap_policy == "inconclusive"
    assert result.schedule_version == SCHEDULE.version
    k1 = by_k(result)[1]
    assert k1.cap_label_window == (label("11:30"), label("11:30"))
    assert k1.crossing_volume == 100_000 and k1.allowance_charged > 0
    assert result.day_verdict_code is None and result.day_integrity_hash is not None
    assert result.volume_band_upper == D("1.02") and result.volume_band_lower is None


def test_R28_deterministic_with_decimal_context_mutated():
    import decimal

    rows = session_rows(t1130=CROSS_ALL_BUY)
    expected = run(rows)[0].result_hash
    saved = decimal.getcontext().prec
    decimal.getcontext().prec = 3
    try:
        assert run(rows)[0].result_hash == expected
    finally:
        decimal.getcontext().prec = saved


# ---- the 22 codes, each reachable ---------------------------------------------------------


def _first_code(results):
    return code_of(results[0])


def _all_code_cases():
    crossing = session_rows(t1130=CROSS_ALL_BUY)
    return {
        ifs.NO_EVIDENCE_FETCHED: lambda: no_evidence_results([attempt()], SCHEDULE, reason="relay_error"),
        ifs.EVIDENCE_FETCHED_TOO_LATE: lambda: run(crossing, fetched_at=at("23:31")),
        ifs.INTERVAL_NOT_APPROVED: lambda: run(crossing, interval="1day"),
        ifs.PRE_SUBMISSION_BAR_OFFERED: None,  # needs a faulty admission rule: see test_R4_pre_submission_bar_offered
        ifs.GAP_IN_SEQUENCE: lambda: run(session_rows(t1200=None)),
        ifs.DUPLICATE_BAR: lambda: run([crossing[0]] + crossing),
        ifs.BAR_ORDER: lambda: run(crossing[::-1]),
        ifs.CAPPED_ANSWER: lambda: run([row("10:00")] * 1000, bhavcopy=None),
        ifs.EMPTY_ANSWER: lambda: run(crossing, empty_flag=True),
        ifs.VOLUME_CHECK_FAILED: lambda: run(crossing, bhavcopy=bhavcopy_for(crossing, total=10)),
        ifs.LABEL_SEMANTICS_UNPROVEN: lambda: run(
            crossing, [attempt(schedule=schedule_with(label_semantics="proven"))], schedule=schedule_with(label_semantics="proven")),
        ifs.VOLUME_REFERENCE_MISSING: lambda: run(crossing, bhavcopy=None),
        ifs.PRICE_RANGE_CHECK_FAILED: lambda: run(
            crossing, bhavcopy=dataclasses.replace(bhavcopy_for(crossing), low=D("399.50"))),
        ifs.EVIDENCE_FETCHED_BEFORE_CLOSE: lambda: run(crossing, fetched_at=at("15:00")),
        ifs.EVIDENCE_SCOPE_MISMATCH: lambda: run(crossing, stock_code="OTHER"),
        ifs.LABEL_MALFORMED: lambda: run([dataclasses.replace(crossing[0], label="bad")] + crossing[1:]),
        ifs.BAR_INCONSISTENT: lambda: run(session_rows(t1130={"low": "401.00"}), bhavcopy=None),
        ifs.SESSION_NOT_STANDARD: lambda: run(crossing, session=None),
        ifs.EVIDENCE_HASH_UNVERIFIED: lambda: run(crossing, sha256_verified=False),
        ifs.NO_ADMISSIBLE_BAR: lambda: run(crossing, [attempt(at_time="15:13", seconds=30)]),
        ifs.SIBLING_UNRESOLVED: lambda: run(
            session_rows(t1500=None), [attempt("a", at_time="10:00"), attempt("b", at_time="15:05")])[1:],
        ifs.SESSION_SCOPE_UNKNOWN: lambda: run(crossing, cas=cas_row(isin="INE0TEST0009")),
    }


def test_every_inconclusive_code_has_a_row():
    assert len(ifs.INCONCLUSIVE_CODES) == 22 and len(set(ifs.INCONCLUSIVE_CODES)) == 22
    assert set(_all_code_cases()) == set(ifs.INCONCLUSIVE_CODES)


@pytest.mark.parametrize("code", [c for c, case in _all_code_cases().items() if case is not None])
def test_inconclusive_code_is_reachable_and_never_a_fill_or_a_miss(code):
    results = _all_code_cases()[code]()
    result = results[0]
    assert code_of(result) == code
    assert {s.outcome for s in result.scenarios} == {IntradayOutcome.INCONCLUSIVE}
    assert {s.reason_code for s in result.scenarios} == {code}
    assert all(s.filled_quantity == 0 and s.fill_price is None for s in result.scenarios)
    assert result.k1_fill_k3_miss is False


# ---- invariants --------------------------------------------------------------------------


def test_invariant_credited_bar_window():
    """I-3: sweep submission times; every credited bar satisfies T - I >= s and T + I <= L."""
    crossing_everywhere = {**CROSS_ALL_BUY, "vol": 1000}
    rows = [row(h, **{k: v for k, v in crossing_everywhere.items()}) for h in hhmm_range()]
    day = assess(rows)
    for hhmm, seconds in itertools.product(["09:15", "09:40", "10:02", "12:59", "15:10", "15:13"], [0, 1, 30, 59]):
        a = attempt(at_time=hhmm, seconds=seconds)
        try:
            admitted = admit_bars(day, a, SCHEDULE)
        except IntradayRefusal:
            continue
        for bar in admitted.bars:
            assert bar.start - timedelta(minutes=1) >= a.submitted_at
            assert bar.start + timedelta(minutes=1) <= at("15:15")


def test_invariant_excluded_bars_inert_given_day_verdict():
    """I-4a: perturb bars before B's submission (day verdict held fixed): B's Stage B outputs do not move."""
    sched = SCHEDULE
    pairs = [(Side.BUY, Side.BUY), (Side.BUY, Side.SELL), (Side.SELL, Side.BUY), (Side.SELL, Side.SELL)]
    limits = {Side.BUY: "399.50", Side.SELL: "400.50"}
    cross = {Side.BUY: CROSS_ALL_BUY, Side.SELL: CROSS_ALL_SELL}
    perturbations = [
        lambda side: {},
        lambda side: {"t1030": {**cross[side], "vol": 5000}},
        lambda side: {"t1030": {**cross[side], "vol": 70_000}, "t1031": {**cross[side], "vol": 3000}},
        lambda side: {"t1015": {**cross[Side.BUY], "vol": 9000}, "t1045": {**cross[Side.SELL], "vol": 9000}},
    ]
    for side_a, side_b in pairs:
        outputs = []
        for perturb in perturbations:
            rows = session_rows(t1130={**cross[side_b], "vol": 8000}, t1200={**cross[side_a], "vol": 8000},
                                **perturb(side_a))
            a = attempt("a", side=side_a, limit=limits[side_a], quantity=60, at_time="10:05", seconds=0)
            b = attempt("b", side=side_b, limit=limits[side_b], quantity=60, at_time="11:00", seconds=0)
            ra, rb = run(rows, [a, b], schedule=sched)
            assert code_of(ra) is None and code_of(rb) is None
            outputs.append([
                (s.k_ticks, s.outcome, s.reason_code, s.filled_quantity, s.fill_price, s.volume_cap,
                 s.allowance_charged, s.cap_label_window, s.crossing_volume) for s in rb.scenarios
            ])
        assert all(out == outputs[0] for out in outputs), (side_a, side_b)
    # The fractional book rides the same sweep.
    build = _fractional_book()
    with_outside, without_outside = build(True), build(False)
    assert [(s.filled_quantity, s.volume_cap) for s in with_outside[1].scenarios] == [
        (s.filled_quantity, s.volume_cap) for s in without_outside[1].scenarios
    ]


def test_invariant_excluded_bar_only_via_day_code():
    """I-4b: raising the 10:01 bar's volume past the R-15 band turns FILLED into INCONCLUSIVE, nothing in between."""
    rows = session_rows(t1130={**CROSS_ALL_BUY, "vol": 100_000})
    ref = bhavcopy_for(rows)
    (filled,) = run(rows, bhavcopy=ref)
    assert by_k(filled)[1].outcome is IntradayOutcome.FILLED
    inflated = [dataclasses.replace(r, volume=r.volume + 100_000) if r.label == label("10:01") else r for r in rows]
    (result,) = run(inflated, bhavcopy=ref)
    assert code_of(result) == ifs.VOLUME_CHECK_FAILED
    assert {s.outcome for s in result.scenarios} == {IntradayOutcome.INCONCLUSIVE}


def test_invariant_day_integrity_independent_of_attempt():
    rows = session_rows(t1130=CROSS_ALL_BUY)
    results = run(rows, [attempt("a", at_time="09:30"), attempt("b", side=Side.SELL, limit="400.50", at_time="13:00",
                                                                quantity=3)])
    assert results[0].day_integrity_hash == results[1].day_integrity_hash == assess(rows).day_hash


def test_stage_a_reads_no_attempt_field():
    import inspect

    params = set(inspect.signature(assess_day).parameters)
    assert not params & {"attempt", "attempts", "submitted_at", "side", "limit_price", "quantity", "k_ticks"}


def test_no_evidence_means_nothing_is_fetched_for_a_fill_request():
    (result,) = no_evidence_results([attempt()], SCHEDULE, reason="route_unavailable")
    assert result.day_integrity_hash is None and result.cas_class is None
    assert {s.outcome for s in result.scenarios} == {IntradayOutcome.INCONCLUSIVE}


# ---- purity scan coverage ----------------------------------------------------------------

COSTS_DIR = Path(__file__).resolve().parents[2] / "backend" / "costs"

# codex-r1-3: the purity scan bans imports and clocks, not file reads. Stage A and Stage B must read nothing from
# disk. Only the two loaders below may touch the filesystem, and each does exactly one read of a committed file.
APPROVED_LOADERS = {"intraday_fills.py": {"load_intraday_scenarios"}, "cas_class.py": {"load_cas_table"}}
FS_ATTR_CALLS = {
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "exists", "is_file", "is_dir", "is_symlink",
    "iterdir", "glob", "rglob", "walk", "scandir", "listdir", "stat", "lstat", "mkdir", "unlink", "rmdir", "touch",
    "rename", "resolve", "absolute", "samefile", "chmod", "symlink_to", "readlink", "home", "cwd", "expanduser",
    "getenv", "environ", "fdopen", "popen", "system",
}
FS_MODULE_NAMES = {"os", "io", "shutil", "tempfile", "glob", "fnmatch", "sys", "subprocess"}


def scan_fs_io(source: str, label: str, approved: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Filesystem operations anywhere outside the ``approved`` function names (module level counts as outside)."""
    import ast

    violations: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _function(self, node) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _function

        def _allowed(self) -> bool:
            return any(name in approved for name in self.stack)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            hit = None
            if isinstance(func, ast.Name) and func.id in {"open", "input", "exec", "eval"}:
                hit = f"{func.id}()"
            elif isinstance(func, ast.Attribute) and func.attr in FS_ATTR_CALLS:
                hit = f".{func.attr}()"
            if hit and not self._allowed():
                where = self.stack[-1] if self.stack else "<module>"
                violations.append(f"{label}:{node.lineno}: filesystem call {hit} in {where}")
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.value, ast.Name) and node.value.id in FS_MODULE_NAMES and not self._allowed():
                where = self.stack[-1] if self.stack else "<module>"
                violations.append(f"{label}:{node.lineno}: use of {node.value.id}.{node.attr} in {where}")
            self.generic_visit(node)

    Visitor().visit(ast.parse(source, filename=label))
    return violations


def plant_in_function(source: str, function: str, statement: str) -> str:
    """Insert ``statement`` as the first line of ``function``'s body: a mutant inside a function only."""
    import ast

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            node.body.insert(0, ast.parse(statement).body[0])
            return ast.unparse(ast.fix_missing_locations(tree))
    raise AssertionError(f"{function} not found")


@pytest.mark.parametrize("module", ["intraday_fills.py", "cas_class.py"])
def test_R30_purity_scan(module):
    from test_costs_purity import scan_source

    path = COSTS_DIR / module
    source = path.read_text(encoding="utf-8")
    assert scan_source(source, module) == []
    # The scanner would catch each of these if someone planted them in the module.
    for plant in (
        "\nimport time\n",
        "\nimport requests\n",
        "\nx = 0.5\n",
        "\nimport datetime as _d\nnow = _d.datetime.now()\n",
        "\nfrom brokers import breeze_gateway\n",
        "\nimport random\n",
    ):
        assert scan_source(source + plant, module), plant


@pytest.mark.parametrize("module", ["intraday_fills.py", "cas_class.py"])
def test_R30_no_filesystem_access_outside_the_approved_loaders(module):
    source = (COSTS_DIR / module).read_text(encoding="utf-8")
    approved = APPROVED_LOADERS[module]
    assert scan_fs_io(source, module, approved) == []
    # The allow-list names real functions that really do the one read, so it cannot rot into a blanket pass.
    defined = {line.split("(")[0].removeprefix("def ") for line in source.splitlines() if line.startswith("def ")}
    assert approved <= defined
    assert scan_fs_io(source, module), "the loaders' own read must be visible to the scanner"


FS_MUTANTS = (
    'Path("/etc/hosts").read_text()',
    'Path("/etc/hosts").read_bytes()',
    'Path("/etc/hosts").exists()',
    'open("/etc/hosts").read()',
    'Path(__file__).resolve()',
    'list(Path(".").iterdir())',
    'list(Path(".").glob("*"))',
    'Path("x").write_text("y")',
    'Path("x").stat()',
    'import_os.getcwd()',
)


@pytest.mark.parametrize("statement", FS_MUTANTS)
@pytest.mark.parametrize(
    "module, function",
    [("intraday_fills.py", "assess_day"), ("intraday_fills.py", "simulate_intraday"),
     ("intraday_fills.py", "admit_bars"), ("cas_class.py", "resolve_cas_class")],
)
def test_R30_filesystem_mutant_inside_a_stage_goes_red(module, function, statement):
    source = (COSTS_DIR / module).read_text(encoding="utf-8")
    approved = APPROVED_LOADERS[module]
    mutant = plant_in_function(source, function, statement.replace("import_os", "os"))
    found = scan_fs_io(mutant, module, approved)
    assert found, f"{statement} inside {function} slipped past the filesystem scan"
    assert any(function in v for v in found), found


def test_R30_filesystem_scan_allows_only_the_named_loader():
    source = (COSTS_DIR / "intraday_fills.py").read_text(encoding="utf-8")
    approved = APPROVED_LOADERS["intraday_fills.py"]
    # The same read is fine in the loader and red anywhere else.
    assert scan_fs_io(plant_in_function(source, "load_intraday_scenarios", 'Path("x").read_text()'), "m", approved) == []
    assert scan_fs_io(plant_in_function(source, "parse_intraday_scenarios", 'Path("x").read_text()'), "m", approved)
    # Module level code is outside the allow-list too.
    assert scan_fs_io(source + '\n_LEAK = Path("x").read_text()\n', "m", approved)
    # A nested helper inside a Stage function is still that Stage's code.
    nested = plant_in_function(source, "assess_day", "def _h():\n    return Path('x').read_text()")
    assert scan_fs_io(nested, "m", approved)
