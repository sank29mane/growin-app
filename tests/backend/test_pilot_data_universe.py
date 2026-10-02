"""PDAT-03 universe filter as of a date, D7 liquidity, surveillance gating and the no-look-ahead proof."""

import ast
import inspect
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from market_data.models import is_valid_isin
from pilot_data import universe as universe_module
from pilot_data.bhavcopy import ingest_pr_zip, ingest_udiff
from pilot_data.constituents import ingest_index_list
from pilot_data.core import PilotDataError, SourceDescriptor, standard_caveats
from pilot_data.models import QuarantineRecord
from pilot_data.nse_ingest import _log_attempt
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore
from pilot_data.surveillance import ingest_surveillance
from pilot_data.targets import TargetMember, TargetUniverseResult
from pilot_data.universe import (
    LiquidityObservation,
    NoTradingStatusSource,
    UniversePolicy,
    _evaluate_universe,
    check_smallcap_exposure,
    classify_smallcap,
    evaluate_universe,
    liquidity_check,
)

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
D = date(2025, 6, 27)  # Friday
D1 = date(2025, 6, 30)  # Monday
POLICY = UniversePolicy()
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def make_isin(n: int) -> str:
    body = f"INE{n:08d}"
    for digit in range(10):
        if is_valid_isin(body + str(digit)):
            return body + str(digit)
    raise AssertionError


def weekdays_ending(end: date, count: int) -> list[date]:
    out, day = [], end
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


SESSIONS = weekdays_ending(D, 75)
WINDOW = SESSIONS[-60:]  # the 60 sessions ending at D


def steady(close="100.00", value="100000000.00", series="EQ"):
    return lambda day: {"close": close, "value": value, "series": series}


def windowed(pattern, *, history=True):
    """Per-index values inside the 60-session window; pre-window sessions are steady history."""
    def rows(day):
        if day in WINDOW:
            result = pattern(WINDOW.index(day))
            return None if result is None else {"close": "100.00", "value": result, "series": "EQ"}
        return {"close": "100.00", "value": "100000000.00", "series": "EQ"} if history else None
    return rows


class World:
    def __init__(self, store, specs):
        self.store = store
        self.specs = specs
        self.isins = {symbol: make_isin(index + 1) for index, symbol in enumerate(specs)}
        self.tokens = {symbol: str(100 + index) for index, symbol in enumerate(specs)}

    def day_rows(self, day):
        rows = []
        for symbol, spec in self.specs.items():
            made = spec(day)
            if made is not None:
                rows.append(
                    kit.udiff_row(symbol, made["series"], made.get("isin", self.isins[symbol]), made["close"],
                                  made["close"], made["close"], made["close"], trade_date=day,
                                  token=made.get("token", self.tokens[symbol]), value=made["value"])
                )
        return rows

    def load(self, days, *, calendar_from=None, skip_marks=()):
        for day in days:
            ingest_udiff(
                self.store,
                SourceDescriptor(source="nse_archive", kind="udiff_cm", locator="https://nsearchives.nseindia.com/u",
                                 fetched_at=FETCHED),
                kit.udiff_zip(day, self.day_rows(day)), trade_date=day,
            )
        mark_calendar(self.store, calendar_from or days[0], days[-1], set(days), skip=set(skip_marks))

    def members(self, as_of):
        return tuple(
            TargetMember(kind="nifty500", anchor_isin=self.isins[s], nse_symbol=s, stock_code=f"C{s}",
                         token=int(self.tokens[s]), company_name=s)
            for s in self.specs
        )

    def targets(self, as_of):
        return TargetUniverseResult(
            workspace="india", caveats=standard_caveats(), as_of=as_of, members=self.members(as_of), exclusions=(),
            etf_rejected=(), master_snapshot="m" * 64, nifty500_snapshot="n" * 64, target_sha256="t" * 64,
        )

    def evaluate(self, day=D, *, targets=None, mode="pilot", before=None, status=None):
        return evaluate_universe(
            self.store, as_of=day, targets=targets or self.targets(D), policy=POLICY, mode=mode,
            allow_missing_surveillance_before=before, workspace="india", status_source=status or NoTradingStatusSource(),
        )


def mark_calendar(store, start, end, data_days, skip=()):
    ensure_fetch_log(store)
    day = start
    while day <= end:
        if day in skip:
            pass
        elif day in data_days:
            _log_attempt(store, d=day, kind="udiff", outcome="ingested", http_status=200, error_code=None,
                         source_sha256=None, url="u", attempted_at=LATER)
        else:
            kinds = ("pr",) if day.weekday() >= 5 else ("pr", "udiff", "cm_legacy")
            for kind in kinds:
                _log_attempt(store, d=day, kind=kind, outcome="no_file", http_status=404, error_code=None,
                             source_sha256=None, url="u", attempted_at=LATER)
        day += timedelta(days=1)


def nse_stamp(day: date) -> str:
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def surveil(store, day, *, asm=(), gsm=()):
    """ASM and GSM snapshots effective on `day`. A dummy symbol keeps the lists non-empty."""
    desc = lambda kind: SourceDescriptor(source="nse_api", kind=kind, locator="https://www.nseindia.com/api/x",
                                         fetched_at=FETCHED)
    asm_rows = [kit.asm_row("ZZZ", None, when=nse_stamp(day))] + [
        kit.asm_row(sym, isin, when=nse_stamp(day)) for sym, isin in asm]
    gsm_rows = [kit.gsm_row("ZZZ", None, when=nse_stamp(day) + " 08:00:00")] + [
        kit.gsm_row(sym, isin, when=nse_stamp(day) + " 08:00:00") for sym, isin in gsm]
    ingest_surveillance(store, desc("surveillance_asm"), kit.asm_json(asm_rows, []), list_name="asm")
    ingest_surveillance(store, desc("surveillance_gsm"), kit.gsm_json(gsm_rows), list_name="gsm")


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def build(store, specs, *, snapshots=True):
    world = World(store, specs)
    world.load(SESSIONS)
    if snapshots:
        surveil(store, D)
    return world


def decision(result, world, symbol):
    return next(d for d in result.decisions if d.anchor_isin == world.isins[symbol])


# ------------------------------------------------------------------ price, series, liquidity boundaries
def test_price_and_median_boundaries(store):
    world = build(store, {
        "P50": steady(close="50.00"), "P49": steady(close="49.95"),
        "M50": steady(value="50000000.00"), "M49": steady(value="49999999.99"),
    })
    result = world.evaluate()
    assert decision(result, world, "P50").eligible and decision(result, world, "P50").reasons == ()
    assert decision(result, world, "P49").reasons == ("price_below_min",)
    assert decision(result, world, "M50").eligible
    assert decision(result, world, "M49").reasons == ("adv_below_min",)
    assert result.eligible_isins == tuple(sorted([world.isins["P50"], world.isins["M50"]]))
    assert result.exclusions_by_reason == {"adv_below_min": 1, "price_below_min": 1}


def test_series_and_missing_bar_rules(store):
    def last_day(series):
        return lambda day: {"close": "100.00", "value": "100000000.00", "series": series if day == D else "EQ"}

    world = build(store, {
        "BEBE": last_day("BE"), "BZBZ": last_day("BZ"), "SMSM": last_day("SM"),
        "NOBAR": lambda day: None if day == D else {"close": "100.00", "value": "100000000.00", "series": "EQ"},
    })
    result = world.evaluate()
    assert decision(result, world, "BEBE").reasons == ("trade_for_trade",)
    assert decision(result, world, "BZBZ").reasons == ("trade_for_trade",)
    assert decision(result, world, "SMSM").reasons == ("series_not_eq",)
    assert decision(result, world, "NOBAR").reasons == ("no_bar_on_date",)


# ------------------------------------------------------------------ D7
def test_d7_known_session_count_and_unknown_never_zero(store):
    world = build(store, {"K39": windowed(lambda i: None if i < 21 else "100000000.00")})
    got = decision(world.evaluate(), world, "K39")
    assert got.reasons == ("insufficient_history",)
    assert (got.known_sessions, got.unknown_sessions) == (39, 21)
    # the no-row sessions are not on any ASM or GSM list, yet stay unknown (absence is not trading evidence)
    assert got.median_traded_value == Decimal("100000000")


class Stub:
    def __init__(self, answer):
        self.answer = answer

    def status(self, isin, session):
        return self.answer


def test_authoritative_status_turns_no_row_sessions_into_known_zero(store):
    world = build(store, {"K39": windowed(lambda i: None if i < 21 else "100000000.00")})
    trading = decision(world.evaluate(status=Stub("trading")), world, "K39")
    assert trading.eligible and (trading.known_sessions, trading.unknown_sessions) == (60, 0)
    for answer in ("suspended", "not_listed", "unknown"):
        kept = decision(world.evaluate(status=Stub(answer)), world, "K39")
        assert kept.known_sessions == 39 and kept.reasons == ("insufficient_history",)


def test_a_session_whose_file_is_quarantined_is_unknown_even_with_a_row(store):
    world = build(store, {"STEADY": steady(), "OTHER": steady()})
    day = WINDOW[10]
    store.record_quarantine([
        QuarantineRecord(workspace="india", check="bhavcopy_consistency", reason_code="udiff_cm_mismatch",
                         scope="raw", isin=world.isins["STEADY"], nse_symbol="STEADY", series="EQ", date_from=day,
                         date_to=day)
    ])
    result = world.evaluate()
    tainted, clean = decision(result, world, "STEADY"), decision(result, world, "OTHER")
    assert (tainted.known_sessions, tainted.unknown_sessions) == (59, 1) and tainted.eligible
    assert (clean.known_sessions, clean.unknown_sessions) == (60, 0)


def test_pre_listing_sessions_are_excluded_not_unknown(store):
    world = build(store, {"LATE": lambda day: {"close": "100.00", "value": "100000000.00", "series": "EQ"}
                          if day >= WINDOW[30] else None})
    got = decision(world.evaluate(), world, "LATE")
    assert got.excluded_prelisting_sessions == 30 and got.known_sessions == 30 and got.unknown_sessions == 0
    assert got.reasons == ("insufficient_history",)


def test_d7_medians_use_known_sessions_for_the_report_and_unknown_as_zero_for_eligibility(store):
    world = build(store, {
        "N45": windowed(lambda i: None if i < 15 else "60000000.00"),
        "N40": windowed(lambda i: None if i < 20 else "60000000.00"),
        "MIX": windowed(lambda i: None if i < 20 else ("40000000.00" if i < 31 else "60000000.00")),
    })
    result = world.evaluate()
    n45, n40, mix = (decision(result, world, s) for s in ("N45", "N40", "MIX"))
    assert (n45.median_traded_value, n45.eligibility_median_traded_value, n45.eligible) == (
        Decimal("60000000"), Decimal("60000000"), True)
    assert n40.eligible and n40.eligibility_median_traded_value == Decimal("60000000")
    assert mix.reasons == ("adv_below_min",)
    assert mix.median_traded_value == Decimal("60000000")  # known sessions only, for reporting
    assert mix.eligibility_median_traded_value == Decimal("40000000")  # unknown counted as zero


def test_unknown_never_makes_ineligible_eligible():
    rng = random.Random(59)
    flipped_to_ineligible = 0
    cases = 0
    for _ in range(600):
        observations = []
        for index in range(60):
            kind = rng.choice(["known_traded", "known_traded", "known_zero", "unknown"])
            value = Decimal(rng.randint(0, 20_000_000_000)) / 100 if kind == "known_traded" else (
                Decimal(0) if kind == "known_zero" else None)
            observations.append(LiquidityObservation(session=date(2025, 1, 1) + timedelta(days=index), kind=kind,
                                                     value=value))
        before = liquidity_check(observations, policy=POLICY)
        eligible = not before.reasons
        cases += 1
        for position, obs in enumerate(observations):
            if obs.kind == "unknown":
                continue
            changed = list(observations)
            changed[position] = LiquidityObservation(session=obs.session, kind="unknown")
            after = liquidity_check(changed, policy=POLICY)
            if not eligible:
                assert after.reasons, "a known-to-unknown change turned an ineligible case eligible"
            elif after.reasons:
                flipped_to_ineligible += 1
            break
    assert cases >= 500 and flipped_to_ineligible > 0  # the converse direction really occurs


# ------------------------------------------------------------------ identity
def test_not_listed_and_unresolved_identity_before_the_lineage_start(store):
    specs = {
        "NEWL": lambda day: {"close": "100.00", "value": "100000000.00", "series": "EQ"} if day == D1 else None,
        "UNRES": lambda day: {"close": "100.00", "value": "100000000.00", "series": "EQ",
                              "isin": make_isin(900 if day < D1 else 901), "token": "77"},
        "OKAY": steady(),
    }
    world = World(store, specs)
    world.isins["UNRES"] = make_isin(901)  # the target is today's (new) ISIN; its old ISIN has no split event
    world.load(SESSIONS + [D1], calendar_from=SESSIONS[0])
    surveil(store, D)
    result = world.evaluate(targets=world.targets(D1))
    assert decision(result, world, "NEWL").reasons == ("not_listed",)
    assert decision(result, world, "UNRES").reasons == ("isin_unresolved_on_date",)
    assert decision(result, world, "UNRES").isin_on_date is None
    assert decision(result, world, "OKAY").eligible


# ------------------------------------------------------------------ surveillance
def test_surveillance_matches_by_isin_and_by_symbol_when_the_isin_is_missing(store):
    world = World(store, {"AAA": steady(), "BBB": steady(), "CCC": steady(), "DDD": steady()})
    world.load(SESSIONS)
    surveil(store, D, asm=[("AAA", world.isins["AAA"]), ("DDD", None)], gsm=[("BBB", world.isins["BBB"])])
    result = world.evaluate()
    assert decision(result, world, "AAA").reasons == ("surveillance_asm",)
    assert decision(result, world, "BBB").reasons == ("surveillance_gsm",)
    assert decision(result, world, "CCC").eligible
    assert decision(result, world, "DDD").reasons == ("surveillance_asm",)  # ISIN-less entry matched by symbol


def test_every_failing_reason_is_recorded_not_only_the_first(store):
    world = World(store, {"MULTI": steady(close="10.00", value="1000000.00", series="BE")})
    world.load(SESSIONS)
    surveil(store, D, asm=[("MULTI", None)])
    reasons = decision(world.evaluate(), world, "MULTI").reasons
    assert reasons == ("trade_for_trade", "price_below_min", "adv_below_min", "surveillance_asm")


def test_surveillance_gate_modes(store):
    world = build(store, {"AAA": steady()}, snapshots=False)
    with pytest.raises(PilotDataError) as pilot:
        world.evaluate(mode="pilot", before=D + timedelta(days=30))  # the allowance is ignored in pilot mode
    assert pilot.value.code == "surveillance_snapshot_missing"
    skipped = world.evaluate(mode="research", before=D + timedelta(days=1))
    assert "SURVEILLANCE_HISTORY_UNAVAILABLE" in [c.code for c in skipped.caveats]
    assert "asm" not in skipped.input_hashes and decision(skipped, world, "AAA").eligible
    with pytest.raises(PilotDataError) as late:
        world.evaluate(mode="research", before=D)  # D is not before the first snapshot date
    assert late.value.code == "surveillance_snapshot_missing"
    with pytest.raises(PilotDataError):
        world.evaluate(mode="research", before=None)


def test_a_stale_snapshot_date_is_not_accepted(store):
    world = build(store, {"AAA": steady()}, snapshots=False)
    surveil(store, D - timedelta(days=1))
    with pytest.raises(PilotDataError) as caught:
        world.evaluate()
    assert caught.value.code == "surveillance_snapshot_missing"


def test_an_unknown_calendar_day_inside_the_lookback_fails_closed(store):
    world = World(store, {"AAA": steady()})
    world.load(SESSIONS, skip_marks={WINDOW[20]})
    surveil(store, D)
    with pytest.raises(PilotDataError) as caught:
        world.evaluate()
    assert caught.value.code == "calendar_unknown_dates"


def test_result_carries_caveats_policy_hash_and_input_hashes(store):
    world = build(store, {"AAA": steady()})
    result = world.evaluate()
    assert [c.code for c in result.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    assert result.policy_sha256 == POLICY.policy_sha256()
    assert set(result.input_hashes) == {"targets", "asm", "gsm", f"lineage:{world.isins['AAA']}"}
    assert store.query("SELECT count(*) FROM universe_evaluations")[0][0] == 1
    assert world.evaluate().result_sha256 == result.result_sha256  # persisting is idempotent
    assert store.query("SELECT count(*) FROM universe_evaluations")[0][0] == 1


# ------------------------------------------------------------------ the no-look-ahead proof
def leak_world(store):
    def a_or_b(day):
        return {"close": "100.00", "value": "100000000.00", "series": "EQ"}

    def c_rows(day):
        if day in WINDOW:
            index = WINDOW.index(day)
            return {"close": "100.00", "value": "40000000.00" if index <= 30 else "60000000.00", "series": "EQ"}
        if day == D1:
            return {"close": "100.00", "value": "10000000000.00", "series": "EQ"}
        return {"close": "100.00", "value": "40000000.00", "series": "EQ"}

    def b_rows(day):
        return {"close": "49.00" if day == D1 else "100.00", "value": "100000000.00", "series": "EQ"}

    world = World(store, {"AAAA": a_or_b, "BBBB": b_rows, "CCCC": c_rows})
    world.load(SESSIONS)
    surveil(store, D)
    return world


def test_appending_later_data_does_not_change_the_result_for_d_but_changes_d_plus_one(store):
    world = leak_world(store)
    targets = world.targets(D)
    honest = world.evaluate(targets=targets)
    assert decision(honest, world, "AAAA").eligible and decision(honest, world, "BBBB").eligible
    assert decision(honest, world, "CCCC").reasons == ("adv_below_min",)
    assert decision(honest, world, "CCCC").eligibility_median_traded_value == Decimal("40000000")
    # append everything dated D+1: the bars, the session, and ASM and GSM snapshots effective D+1
    world.load([D1], calendar_from=D + timedelta(days=1))
    surveil(store, D1, asm=[("AAAA", world.isins["AAAA"])])
    again = world.evaluate(targets=targets)
    assert again.result_sha256 == honest.result_sha256 and again.decisions == honest.decisions
    next_day = world.evaluate(D1, targets=targets)
    assert decision(next_day, world, "AAAA").reasons == ("surveillance_asm",)
    assert decision(next_day, world, "BBBB").reasons == ("price_below_min",)
    assert decision(next_day, world, "CCCC").eligible
    assert decision(next_day, world, "CCCC").eligibility_median_traded_value == Decimal("50000000")
    assert next_day.result_sha256 != honest.result_sha256


def test_a_deliberate_leak_is_visible_in_the_hash(store):
    world = leak_world(store)
    targets = world.targets(D)
    honest = world.evaluate(targets=targets)
    world.load([D1], calendar_from=D + timedelta(days=1))
    surveil(store, D1, asm=[("AAAA", world.isins["AAAA"])])
    leaky = _evaluate_universe(
        store, as_of=D, data_cutoff=D1, targets=targets, policy=POLICY, mode="pilot",
        allow_missing_surveillance_before=None, workspace="india", status_source=NoTradingStatusSource(),
    )
    assert leaky.as_of == D  # labelled as D, yet it selected D+1 data
    assert decision(leaky, world, "AAAA").reasons == ("surveillance_asm",)
    assert decision(leaky, world, "BBBB").reasons == ("price_below_min",)
    assert decision(leaky, world, "CCCC").eligible
    assert leaky.result_sha256 != honest.result_sha256


def test_production_api_has_no_cutoff_parameter_and_no_other_caller_of_the_private_function():
    assert "data_cutoff" not in inspect.signature(evaluate_universe).parameters
    package = Path(universe_module.__file__).parent
    callers = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "_evaluate_universe":
                    callers.append((path.name, fn.name))
    assert callers == [("universe.py", "evaluate_universe")]


# ------------------------------------------------------------------ small caps and the 30 percent rule
def lists(store, nifty_isins, small_isins):
    def rows(isins, total, base):
        extra = [make_isin(base + n) for n in range(total - len(isins))]
        return [{"Company Name": f"C{i}", "Industry": "I", "Symbol": f"X{i}", "Series": "EQ", "ISIN Code": isin}
                for i, isin in enumerate([*isins, *extra])]

    desc = SourceDescriptor(source="nse_archive", kind="index_list_nifty500",
                            locator="https://nsearchives.nseindia.com/l", fetched_at=FETCHED)
    ingest_index_list(store, desc, kit.index_list_csv(rows(nifty_isins, 500, 50_000)), list_name="nifty500")
    ingest_index_list(store, desc, kit.index_list_csv(rows(small_isins, 250, 60_000)), list_name="smallcap250")


def member(kind, symbol, n):
    return TargetMember(kind=kind, anchor_isin=make_isin(n), nse_symbol=symbol, stock_code=f"C{symbol}", token=n,
                        company_name=symbol)


def targets_of(*members):
    return TargetUniverseResult(
        workspace="india", caveats=standard_caveats(), as_of=D, members=tuple(members), exclusions=(), etf_rejected=(),
        master_snapshot="m" * 64, nifty500_snapshot="n" * 64, target_sha256="t" * 64,
    )


def etf_info(store, *rows):
    ingest_pr_zip(
        store, SourceDescriptor(source="nse_archive", kind="pr_zip", locator="https://nsearchives.nseindia.com/pr",
                                fetched_at=FETCHED),
        kit.pr_zip(D, [kit.pd_index_row()], [], list(rows)), trade_date=D,
    )


def test_classification_follows_d08(store):
    small, mid, odd = member("nifty500", "SMALLCO", 1), member("nifty500", "MIDCO", 2), member("nifty500", "ODDCO", 3)
    etf_small = member("liquid_etf", "SMLETF", 4)
    etf_other = member("liquid_etf", "BIGETF", 5)
    etf_none = member("liquid_etf", "NOINFO", 6)
    targets = targets_of(small, mid, odd, etf_small, etf_other, etf_none)
    lists(store, [small.anchor_isin, mid.anchor_isin], [small.anchor_isin])
    etf_info(store, kit.etf_row("SMLETF", "SMALL ETF", "Nifty Smallcap 250 TRI"),
             kit.etf_row("BIGETF", "BIG ETF", "NIFTY 50"))
    assert classify_smallcap(store, targets) == {
        small.anchor_isin: "small", mid.anchor_isin: "not_small", odd.anchor_isin: "unclassified",
        etf_small.anchor_isin: "small", etf_other.anchor_isin: "not_small", etf_none.anchor_isin: "unclassified",
    }


def test_microcap_underlying_counts_as_small(store):
    etf = member("liquid_etf", "MICETF", 8)
    lists(store, [], [])
    etf_info(store, kit.etf_row("MICETF", "MICRO ETF", "NIFTY MICROCAP 250"))
    assert classify_smallcap(store, targets_of(etf)) == {etf.anchor_isin: "small"}


def test_a_missing_list_leaves_everything_unclassified_which_counts_toward_the_cap(tmp_path):
    with PilotDataStore(tmp_path / "bare", workspace="india") as bare:
        got = classify_smallcap(bare, targets_of(member("nifty500", "ANY", 9)))
    assert got == {make_isin(9): "unclassified"}


def exposure(values, classification):
    return check_smallcap_exposure(values, capital=Decimal("50000"), classification=classification, policy=POLICY,
                                   workspace="india")


def test_exposure_cap_is_exactly_thirty_percent():
    a, b, c = make_isin(1), make_isin(2), make_isin(3)
    classes = {a: "small", b: "unclassified", c: "not_small"}
    exact = exposure({a: Decimal("10000.00"), b: Decimal("5000.00"), c: Decimal("35000.00")}, classes)
    assert exact.passed and exact.small_value == Decimal("15000.00") and exact.small_share == Decimal("0.3")
    over = exposure({a: Decimal("10000.01"), b: Decimal("5000.00"), c: Decimal("34999.99")}, classes)
    assert not over.passed and over.counted_isins == tuple(sorted([a, b]))
    absent = exposure({make_isin(99): Decimal("15000.01")}, classes)
    assert not absent.passed and absent.counted_isins == (make_isin(99),)


def test_exposure_rejects_bad_inputs_and_carries_caveats():
    with pytest.raises(PilotDataError) as capital:
        check_smallcap_exposure({}, capital=Decimal("0"), classification={}, policy=POLICY, workspace="india")
    assert capital.value.code == "exposure_capital_invalid"
    with pytest.raises(PilotDataError) as value:
        exposure({make_isin(1): Decimal("-1")}, {})
    assert value.value.code == "exposure_value_invalid"
    checked = exposure({make_isin(1): Decimal("1")}, {make_isin(1): "not_small"})
    assert [c.code for c in checked.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"] and checked.passed


def test_decisions_carry_the_real_classification(store):
    world = build(store, {"AAA": steady(), "BBB": steady()})
    lists(store, [world.isins["AAA"], world.isins["BBB"]], [world.isins["BBB"]])
    result = world.evaluate()
    assert decision(result, world, "AAA").smallcap_class == "not_small"
    assert decision(result, world, "BBB").smallcap_class == "small"
