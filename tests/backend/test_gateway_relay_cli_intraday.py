"""probe-intraday step and compare (64-02 Task 2, design section 11).

A fake relay stands in for the VM: it counts calls the way the relay's day counter does and
records every request. No network, no tunnel, no real credentials.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import time as clock
from datetime import date, datetime, time, timezone
from pathlib import Path

import httpx
import pytest

from brokers.breeze_gateway import cli as cli_mod
from brokers.breeze_gateway import client as client_mod
from brokers.breeze_gateway import intraday_probe as probe
from brokers.breeze_gateway.client import CONTRACT, RelayBudget, UpstreamPayload
from brokers.breeze_gateway.errors import IntradayRouteUnavailable, UpstreamError

import pilot_data_testkit as kit

TODAY = date(2026, 10, 21)
HOLIDAY = date(2026, 10, 2)
RID = "0123456789abcdef"
FETCHED_UTC = datetime(2026, 10, 21, 10, 30, tzinfo=timezone.utc)  # 16:00 IST
START = 100  # calls_today_ist when the probe begins


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def minute_rows(count: int = 375, volume: int = 10, high: str = "101", low: str = "99") -> list[dict]:
    rows = []
    for n in range(count):
        minute = 9 * 60 + 15 + n
        rows.append(
            {
                "datetime": f"2026-10-21 {minute // 60:02d}:{minute % 60:02d}:00",
                "stock_code": "RELIND",
                "exchange_code": "NSE",
                "open": "100",
                "high": high,
                "low": low,
                "close": "100",
                "volume": str(volume),
            }
        )
    return rows


def body_of(rows: list[dict]) -> bytes:
    return json.dumps({"Success": rows, "Status": 200, "Error": None}, separators=(",", ":")).encode()


def make_payload(body: bytes, *, route: str = "/v1/bars/intraday", symbol: str = "RELIND", empty: bool = False):
    return UpstreamPayload(
        body=body,
        sha256=sha(body),
        fetched_at_utc=FETCHED_UTC,
        upstream_api=client_mod.API_BARS_V2,
        upstream_request={"interval": "1minute", "stock_code": symbol},
        upstream_interval="1minute",
        breeze_status=200,
        empty=empty,
        upstream_last_modified=None,
        request_id=RID,
        route=route,
        query={"stock_code": symbol},
        contract=CONTRACT,
    )


def empty_payload(symbol: str = "RELIND") -> UpstreamPayload:
    return make_payload(body_of([]), symbol=symbol, empty=True)


class FakeRelay:
    """Counts like the relay: every Breeze-backed call adds one to calls_today_ist."""

    def __init__(self, *, counted: int = START, answers: dict | None = None, bump_before_send: dict | None = None):
        self.counted = counted
        self.calls: list[tuple[str, str, date, int]] = []  # kind, symbol, day, delta at send
        self.answers = answers or {}
        self.bump = bump_before_send or {}
        self.baseline = counted
        self.budget_reads = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def budget(self) -> RelayBudget:
        self.budget_reads += 1
        return RelayBudget(
            calls_last_minute=0,
            calls_today_ist=self.counted,
            per_minute_limit=80,
            per_day_limit=4500,
            remaining_today=4500 - self.counted,
            ist_date=TODAY,
        )

    def _send(self, kind: str, symbol: str, day: date) -> UpstreamPayload:
        self.counted += self.bump.get(len(self.calls) + 1, 0)  # other callers hit the relay too
        self.counted += 1
        self.calls.append((kind, symbol, day, self.counted - self.baseline))
        queue = self.answers.get((kind, symbol))
        outcome = queue.pop(0) if queue else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        if kind == "quote":
            row = {"stock_code": symbol, "ltt": "21-Oct-2026 14:00:01", "total_quantity_traded": "5000",
                   "high": "101", "low": "99"}
            return make_payload(body_of([row]), route="/v1/quotes", symbol=symbol)
        if kind == "daily":
            row = {"datetime": "2026-10-21 00:00:00", "volume": "3750", "high": "101", "low": "99"}
            return make_payload(body_of([row]), route="/v1/bars/daily", symbol=symbol)
        return make_payload(body_of(minute_rows()), symbol=symbol)

    def intraday_bars(self, stock_code, session_date, *, interval="1minute", exchange="NSE"):
        return self._send("intraday", stock_code, session_date)

    def quote(self, stock_code, *, exchange="NSE"):
        return self._send("quote", stock_code, TODAY)

    def daily_bars(self, stock_code, from_date, to_date, *, exchange="NSE"):
        return self._send("daily", stock_code, from_date)


class Env:
    def __init__(self, tmp_path: Path, relay: FakeRelay) -> None:
        self.dir = tmp_path / "probe"
        self.relay = relay
        self.sleeps: list[float] = []

    def step(self, step: str, *extra: str) -> tuple[int, list[str]]:
        out = io.StringIO()
        code = cli_mod.main(
            ["probe-intraday", "step", "--step", step, "--probe-dir", str(self.dir), *extra],
            client_factory=lambda: self.relay,
            out=out,
            sleep=self.sleeps.append,
        )
        return code, out.getvalue().splitlines()

    def ledger(self) -> dict:
        return json.loads((self.dir / f"ledger-{TODAY.isoformat()}.json").read_text())


@pytest.fixture()
def env(tmp_path) -> Env:
    return Env(tmp_path, FakeRelay())


def sent(env: Env) -> int:
    return len(env.relay.calls)


# ------------------------------------------------------------------ the stop rule


def test_probe_stop_before_21st_request(env: Env):
    env.step("S-3")
    env.relay.counted += 16  # other callers: the relay delta is now 19
    code, lines = env.step("S-4")
    assert code == 0 and sent(env) == 4  # the 20th request fits
    code, lines = env.step("S-6")
    assert code == 0
    assert sent(env) == 4  # the 21st is never sent
    assert any(line == "slot S-6 intraday RELIND probe_budget_stop" for line in lines)
    assert env.ledger()["stopped"] is True
    assert max(delta for *_, delta in env.relay.calls) <= probe.HARD_STOP
    # nothing more is sent that day, whatever the step
    for step, extra in (("S-7", ["--symbol", "TCS"]), ("S-9", []), ("S-10", [])):
        env.step(step, *extra)
    assert sent(env) == 4


def test_probe_stop_reads_the_budget_before_every_charged_request(env: Env):
    env.step("S-3")
    assert env.relay.budget_reads >= 1 + 3  # the baseline read, then one read per request


def test_probe_retry_needs_base_slots(env: Env):
    # S-3, first symbol empty. After the base send the relay delta is 6 or 7.
    # A retry needs spent + 1 + remaining base (13) <= 20: delta 6 passes, delta 7 does not.
    allowed = Env(env.dir.parent / "a", FakeRelay(answers={("intraday", "RELIND"): [empty_payload()]},
                                                  bump_before_send={1: 5}))
    _, lines = allowed.step("S-3")
    assert [c[:2] for c in allowed.relay.calls].count(("intraday", "RELIND")) == 2  # retried
    assert not any("probe_retry_skipped" in line for line in lines)

    refused = Env(env.dir.parent / "b", FakeRelay(answers={("intraday", "RELIND"): [empty_payload()]},
                                                  bump_before_send={1: 6}))
    _, lines = refused.step("S-3")
    assert [c[:2] for c in refused.relay.calls].count(("intraday", "RELIND")) == 1  # first answer kept
    assert any("probe_retry_skipped" in line for line in lines)
    assert refused.ledger()["stopped"] is False  # a skipped retry does not stop later base steps
    assert any(line.startswith("answer S-3 intraday RELIND") for line in lines)


def test_probe_repeated_steps_cannot_starve_the_base_slots(env: Env):
    for _ in range(8):
        env.step("S-3")  # the first run is base, every repeat spends a retry slot
    assert sent(env) == 9  # 3 base + 6 retry slots
    env.step("S-4")
    env.step("S-5")
    env.step("S-6")
    env.step("S-7", "--symbol", "TCS")
    env.step("S-8", "--date", HOLIDAY.isoformat())
    env.step("S-9")
    env.step("S-10")
    assert sent(env) == 20
    assert env.ledger()["stopped"] is False
    assert max(delta for *_, delta in env.relay.calls) == 20


def test_probe_baseline_persists_across_runs(env: Env):
    env.step("S-3")
    assert env.ledger()["baseline"] == START
    env.relay.counted += 50  # another caller; a re-run must not re-baseline to the new count
    _, lines = env.step("S-3")
    assert env.ledger()["baseline"] == START
    assert sent(env) == 3  # relay delta 53: the repeat is refused
    assert any("probe_retry_skipped" in line for line in lines)
    assert env.ledger()["sent"] == 3


def test_probe_a_step_other_than_s3_needs_the_baseline(env: Env):
    code, lines = env.step("S-4")
    assert code == cli_mod.EXIT_USAGE and lines == ["probe_error probe_baseline_missing"]
    assert sent(env) == 0 and not env.dir.exists()


def test_probe_relay_delta_stops_earlier(env: Env):
    env.step("S-3")  # ledger sent 3, relay delta 3
    env.relay.counted += 15  # relay delta 18, ledger says 3
    env.step("S-4")
    code, lines = env.step("S-5")
    assert sent(env) == 5  # S-4 (delta 19) and the first quote (delta 20); the second quote is refused
    assert any("probe_budget_stop" in line for line in lines)
    assert env.ledger()["sent"] == 5
    _, closing = env.step("S-12")
    assert "step S-12 relay_delta 20 ledger_sent 5" in closing and "step S-12 counts DIFFER" in closing


def test_probe_s12_counts_match_when_nobody_else_called(env: Env):
    env.step("S-3")
    _, lines = env.step("S-12")
    assert lines == ["step S-12 relay_delta 3 ledger_sent 3", "step S-12 counts match"]
    assert sent(env) == 3  # S-12 reads the budget only


def test_probe_s8_never_retried(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [empty_payload()]
    code, lines = env.step("S-8", "--date", HOLIDAY.isoformat())
    assert code == 0 and sent(env) == 4  # one request, the empty answer is the result
    assert any("empty true" in line for line in lines)
    env.relay.answers[("intraday", "RELIND")] = [UpstreamError("UPSTREAM_ERROR", http_status=502)]
    env.step("S-8", "--date", HOLIDAY.isoformat())
    assert sent(env) == 5  # an upstream error is not retried either


def test_probe_other_steps_retry_once_on_empty_and_once_on_a_5xx(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [empty_payload(), empty_payload(), empty_payload()]
    env.step("S-4")
    assert sent(env) == 5  # one retry, never a second
    env.relay.answers[("intraday", "RELIND")] = [UpstreamError("UPSTREAM_ERROR", http_status=502)]
    env.step("S-9")
    assert sent(env) == 7  # the 5xx was counted and retried once


def test_probe_s8_validation(env: Env):
    env.step("S-3")
    assert env.step("S-8")[1] == ["probe_error probe_holiday_required"]
    assert env.step("S-8", "--date", TODAY.isoformat())[1] == ["probe_error probe_holiday_not_past"]
    assert env.step("S-7")[1] == ["probe_error probe_symbol_required"]
    assert sent(env) == 3


def test_probe_quote_and_daily_steps_counted(env: Env):
    env.step("S-3")
    _, quote_lines = env.step("S-5")
    _, daily_lines = env.step("S-10")
    assert [c[0] for c in env.relay.calls[3:]] == ["quote", "quote", "daily", "daily"]
    assert env.ledger()["sent"] == 7  # every charged request goes through the one ledger
    assert env.sleeps == [probe.QUOTE_GAP_S]  # the 60 s gap between the two quotes
    assert any(line.startswith("answer S-5 quote RELIND") and "total_quantity_traded 5000" in line
               for line in quote_lines)
    assert any("today_row true today_volume 3750" in line for line in daily_lines)
    assert [c[1] for c in env.relay.calls[5:]] == ["RELIND", "NIFBEE"]


def test_probe_full_checklist_is_fourteen_base_calls(env: Env):
    env.step("S-3")
    env.step("S-4")
    env.step("S-5")
    env.step("S-6")
    env.step("S-7", "--symbol", "TCS")
    env.step("S-8", "--date", HOLIDAY.isoformat())
    env.step("S-9")
    env.step("S-10")
    assert sent(env) == probe.BASE_CALLS == 14
    _, lines = env.step("S-12")
    assert lines[-1] == "step S-12 counts match"


def test_probe_never_reaches_an_order_route():
    import ast

    tree = ast.parse(Path(probe.__file__).read_text())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    # the only relay methods the probe calls are the three charged reads and the free budget read
    assert {"intraday_bars", "quote", "daily_bars", "budget"} <= names
    assert not names & {"preview_order", "OrdersClient", "orders", "login_arm", "login_complete", "revoke"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "OrdersClient" not in imported and "RelayClient" not in imported  # no client is built here


# ------------------------------------------------- r2: concurrency, S-7 pinning, exhausted errors


class SlowRelay(FakeRelay):
    """A relay whose charged calls take a moment, so two invocations really overlap."""

    def _send(self, kind: str, symbol: str, day: date) -> UpstreamPayload:
        clock.sleep(0.15)
        return super()._send(kind, symbol, day)


def run_concurrently(first, second) -> None:
    gate = threading.Barrier(2)

    def go(call):
        gate.wait()
        call()

    threads = [threading.Thread(target=go, args=(call,)) for call in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()


def test_probe_two_concurrent_invocations_cannot_both_pass_the_check(tmp_path: Path):
    relay = SlowRelay()
    first = Env(tmp_path, relay)
    first.step("S-3")
    relay.counted += 16  # other callers: the relay delta is 19, the ledger says 3
    second = Env(tmp_path, relay)  # a second invocation: same probe dir, same relay
    run_concurrently(lambda: first.step("S-4"), lambda: second.step("S-9"))
    assert len(relay.calls) == 4  # S-3's three plus exactly one of S-4 and S-9
    assert max(delta for *_, delta in relay.calls) == probe.HARD_STOP  # never the 21st
    ledger = first.ledger()
    assert ledger["stopped"] is True and ledger["sent"] == 4
    assert relay.counted - relay.baseline == probe.HARD_STOP


def test_probe_the_ledger_lock_is_a_real_cross_process_flock(env: Env, tmp_path: Path):
    import fcntl
    import os

    env.step("S-3")
    lock = env.dir / f"ledger-{TODAY.isoformat()}.lock"
    held = os.open(lock, os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)  # another invocation is mid-request
    try:
        done = threading.Event()
        worker = threading.Thread(target=lambda: (env.step("S-4"), done.set()))
        worker.start()
        assert not done.wait(0.3)  # blocked behind the holder, nothing sent
        assert len(env.relay.calls) == 3
    finally:
        os.close(held)
    worker.join(timeout=20)
    assert done.is_set() and len(env.relay.calls) == 4


def test_probe_s7_with_eight_symbols_cannot_starve_s10(env: Env):
    for step in ("S-3", "S-4", "S-5", "S-6"):
        env.step(step)
    assert sent(env) == 9
    for code in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"):
        env.step("S-7", "--symbol", code)
    assert sent(env) == 16  # one base call, then retries only while the later base steps keep their slots
    ledger = env.ledger()
    assert ledger["stopped"] is False
    env.step("S-8", "--date", HOLIDAY.isoformat())
    env.step("S-9")
    code, lines = env.step("S-10")
    assert code == 0 and any("today_row true" in line for line in lines)
    assert sent(env) == probe.HARD_STOP
    assert [c[0] for c in env.relay.calls[-2:]] == ["daily", "daily"]  # S-10 ran both its base calls
    assert max(delta for *_, delta in env.relay.calls) <= probe.HARD_STOP


def test_probe_s7_another_symbol_is_a_retry_not_a_second_base_call(env: Env):
    env.step("S-3")
    env.step("S-7", "--symbol", "AAA")
    base = env.ledger()["steps"]["S-7"]["slots"]
    assert list(base) == ["intraday-s7"] and base["intraday-s7"]["requests"] == 1
    env.step("S-7", "--symbol", "BBB")
    assert env.ledger()["steps"]["S-7"]["slots"]["intraday-s7"]["requests"] == 2
    assert [c[1] for c in env.relay.calls[3:]] == ["AAA", "BBB"]


def test_probe_exhausted_upstream_errors_are_inconclusive_and_exit_nonzero(env: Env):
    env.step("S-3")
    boom = UpstreamError("UPSTREAM_ERROR", http_status=502)
    env.relay.answers[("intraday", "RELIND")] = [boom, UpstreamError("UPSTREAM_ERROR", http_status=502)]
    code, lines = env.step("S-9")
    assert code == cli_mod.EXIT_RELAY != 0
    assert sent(env) == 5  # tried twice, never a third time
    assert lines[-1] == "step S-9 status INCONCLUSIVE sent 5"
    assert not any("status complete" in line for line in lines)
    assert not any(line.startswith("answer S-9") for line in lines)


def test_probe_a_retry_that_recovers_is_not_inconclusive(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [UpstreamError("UPSTREAM_ERROR", http_status=502)]
    code, lines = env.step("S-9")
    assert code == 0 and lines[-1] == "step S-9 status complete sent 5"


def test_probe_s8_error_is_inconclusive_with_a_nonzero_exit(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [UpstreamError("UPSTREAM_ERROR", http_status=502)]
    code, lines = env.step("S-8", "--date", HOLIDAY.isoformat())
    assert code != 0 and lines[-1] == "step S-8 status INCONCLUSIVE sent 4"


def test_probe_an_error_whose_retry_is_not_affordable_is_inconclusive_not_retry_skipped(env: Env):
    env.step("S-3")
    env.relay.counted += 14  # relay delta 17: a base call fits, a retry (needs 1 + 12 more) does not
    env.relay.answers[("intraday", "RELIND")] = [UpstreamError("UPSTREAM_ERROR", http_status=502)]
    code, lines = env.step("S-4")
    assert code != 0
    assert lines[-1].startswith("step S-4 status INCONCLUSIVE")
    assert "slot S-4 intraday RELIND probe_retry_skipped" in lines


def test_probe_the_stop_latch_survives_a_new_invocation_even_when_the_budget_would_allow(env: Env):
    env.step("S-3")
    env.relay.counted += 17  # delta 20: the next base call is refused
    env.step("S-4")
    assert env.ledger()["stopped"] is True and sent(env) == 3
    env.relay.counted = START  # the relay now reads as if nothing had been spent
    for step, extra in (("S-6", []), ("S-7", ["--symbol", "TCS"]), ("S-10", [])):
        code, lines = env.step(step, *extra)
        assert code == 0
    assert sent(env) == 3  # only the latch holds the probe back
    assert env.ledger()["stopped"] is True


def test_probe_a_base_stop_sets_the_latch_in_the_ledger_file(env: Env):
    env.step("S-3")
    assert env.ledger()["stopped"] is False
    env.relay.counted += 17
    env.step("S-4")
    persisted = json.loads((env.dir / f"ledger-{TODAY.isoformat()}.json").read_text())
    assert persisted["stopped"] is True
    assert persisted["steps"]["S-4"]["slots"]["intraday-RELIND"]["evidence"][f"RELIND@{TODAY.isoformat()}"]["result"] == probe.BUDGET_STOP


# ------------------------------------------------------------ saved answers and printed lines


def test_probe_answers_saved_on_mac_only(env: Env, tmp_path: Path):
    _, lines = env.step("S-3")
    answers = env.dir / "answers" / TODAY.isoformat()
    bodies = sorted(answers.glob("*.body"))
    metas = sorted(answers.glob("*.meta.json"))
    assert len(bodies) == len(metas) == 3
    expected = body_of(minute_rows())
    for body_path, meta_path in zip(bodies, metas):
        assert body_path.read_bytes() == expected  # the exact response bytes
        meta = json.loads(meta_path.read_text())
        assert meta["sha256"] == sha(expected)
        assert meta["upstream_interval"] == "1minute"
        assert meta["provenance"]["fetched_at_utc"] == "2026-10-21T10:30:00.000Z"
        assert meta["step"] == "S-3" and meta["empty"] is False
    written = {p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {"probe"}  # nothing written anywhere but the probe directory
    assert {p.name for p in env.dir.iterdir()} == {
        f"ledger-{TODAY.isoformat()}.json",
        f"ledger-{TODAY.isoformat()}.lock",
        "answers",
    }
    blob = "\n".join(lines)
    assert RID not in blob and "http" not in blob and "token" not in blob.lower()


def test_probe_printed_lines_carry_the_s6_facts(env: Env):
    env.step("S-3")
    _, lines = env.step("S-6")
    line = next(line for line in lines if line.startswith("answer S-6 intraday RELIND"))
    for needle in (
        "rows 375", 'first_label "2026-10-21 09:15:00"', 'last_label "2026-10-21 15:29:00"',
        "empty false", "capped false", "sha256 verified", "volume_sum_all 3750",
        "volume_sum_day_window 3750", "after_1514 15", "after_1515 14", "after_1529 0", "after_1535 0",
        "zero_volume_rows 0", "single_price_rows 0", "max_high 101", "min_low 99",
    ):
        assert needle in line, needle
    assert lines[-1].startswith("step S-6 status complete")


def test_probe_flags_a_capped_and_an_empty_answer(env: Env):
    capped = make_payload(body_of(minute_rows(1000)))
    env.relay.answers[("intraday", "RELIND")] = [capped]
    _, lines = env.step("S-3")
    assert any("rows 1000" in line and "capped true" in line for line in lines)


def test_probe_relay_errors_are_typed_and_the_call_stays_counted(env: Env):
    env.relay.answers[("intraday", "RELIND")] = [IntradayRouteUnavailable(
        "NOT_FOUND", http_status=404, reason="route_unavailable", request_id=RID)]
    code, lines = env.step("S-3")
    assert code == cli_mod.EXIT_RELAY
    assert lines == [f"NOT_FOUND (request {RID}) reason route_unavailable"]
    assert sent(env) == 1  # one try, no retry, no daily fallback
    assert env.ledger()["sent"] == 1


def test_probe_counts_a_request_before_it_goes_out(env: Env):
    env.relay.answers[("intraday", "RELIND")] = [KeyboardInterrupt()]
    with pytest.raises(KeyboardInterrupt):
        env.step("S-3")
    assert env.ledger()["sent"] == 1  # a crash mid-request leaves it counted


def test_probe_refuses_an_unreadable_or_foreign_ledger(env: Env):
    env.step("S-3")
    ledger = env.dir / f"ledger-{TODAY.isoformat()}.json"
    ledger.write_text("{not json")
    assert env.step("S-4")[1] == ["probe_error ledger_unreadable"]
    ledger.write_text(json.dumps({"schema": "other"}))
    assert env.step("S-4")[1] == ["probe_error ledger_invalid"]
    assert sent(env) == 3


def test_probe_rejects_an_unknown_step(env: Env):
    out = io.StringIO()
    code = cli_mod.main(["probe-intraday", "step", "--step", "S-11", "--probe-dir", str(env.dir)],
                        client_factory=lambda: env.relay, out=out)
    assert code == cli_mod.EXIT_USAGE and sent(env) == 0


def test_summarise_bars_reads_labels_as_given_and_flags_oddities():
    window = (time(9, 15), time(15, 35))
    rows = [
        {"datetime": "2026-10-21 09:14:00", "volume": "5", "high": "10", "low": "10"},
        {"datetime": "2026-10-21 09:15:00", "volume": "0", "high": "11", "low": "9"},
        {"datetime": "2026-10-21 15:35:00", "volume": "7", "high": "12", "low": "8"},
        {"datetime": "2026-10-21 15:36:00", "volume": "3", "high": "13", "low": "7"},
        {"datetime": "garbage", "volume": "x", "high": "1", "low": "1"},
        "not a row",
    ]
    facts = probe.summarise_bars(body_of(rows), window)
    assert facts["rows"] == 6 and facts["bad_rows"] == 2
    assert facts["volume_all"] == 15 and facts["volume_day_window"] == 7  # 09:15 and 15:35 are inside D
    assert facts["zero_volume"] == 1 and facts["single_price"] == 1
    assert facts["after"] == {"1514": 2, "1515": 2, "1529": 2, "1535": 1}
    assert (facts["max_high"], facts["min_low"]) == (13, 7)
    assert probe.summarise_bars(b'{"Success": null}', window) is None
    assert probe.summarise_bars(b"not json", window) is None


def test_the_day_window_is_the_committed_r15_window():
    assert probe.day_window() == (time(9, 15), time(15, 35))


# ------------------------------------------------------------------------ compare (S-11b)

D = TODAY
PILOT_FETCHED = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)


def nse_desc(kind: str):
    from pilot_data.core import SourceDescriptor

    return SourceDescriptor(source="nse_archive", kind=kind, locator=f"https://nsearchives.nseindia.com/{kind}",
                            fetched_at=PILOT_FETCHED)


@pytest.fixture()
def pilot_root(tmp_path) -> Path:
    from pilot_data.bhavcopy import ingest_cm_legacy
    from pilot_data.security_master import ingest_security_master
    from pilot_data.store import PilotDataStore

    root = tmp_path / "pilot"
    with PilotDataStore(root, workspace="india") as store:
        ingest_security_master(
            store,
            nse_desc("security_master"),
            kit.security_master_bytes([kit.MASTER_RELIND, kit.MASTER_NIFBEE]),
            snapshot_date=D,
            snapshot_date_basis="test_fixture",
        )
        rows = [
            kit.cm_legacy_row("RELIANCE", "EQ", "INE002A01018", "100", "101", "99", "100", trade_date=D,
                              volume="3750"),
            kit.cm_legacy_row("NIFTYBEES", "EQ", "INF204KB14I2", "100", "101", "98", "100", trade_date=D,
                              volume="7500"),
        ]
        ingest_cm_legacy(store, nse_desc("cm_legacy"), kit.cm_legacy_zip(D, rows), trade_date=D)
    return root


def cas_table_file(tmp_path: Path, isin: str = "INE002A01018") -> Path:
    path = tmp_path / "cas.json"
    path.write_text(json.dumps({
        "schema": "growin.costs.cas_class/1", "source_id": "test-fixture", "status": "TEST FIXTURE",
        "versions": [{
            "version": "v1", "revision": "r1", "effective_from": "2026-01-01", "effective_to": None,
            "publication_id": "TEST-PUB", "publication_date": "2026-01-01", "source_sha256": "a" * 64,
            "operator_approval": {"line": "approved by test", "date": "2026-01-02"},
            "rows": [{"isin": isin, "symbol": "RELIANCE", "class": "cas"}],
        }],
    }))
    return path


def compare(env: Env, pilot_root: Path, *extra: str) -> tuple[int, list[str]]:
    out = io.StringIO()
    code = cli_mod.main(
        ["probe-intraday", "compare", "--saved", str(env.dir), "--pilot-store", str(pilot_root), *extra],
        client_factory=lambda: pytest.fail("compare must not build a client"),
        out=out,
    )
    return code, out.getvalue().splitlines()


def test_probe_compare_offline_no_client(env: Env, pilot_root: Path, monkeypatch):
    env.step("S-3")
    env.step("S-6")

    def sentinel(*args, **kwargs):
        raise AssertionError("compare touched the network or built a relay client")

    monkeypatch.setattr(client_mod.RelayClient, "open", classmethod(lambda cls, *a, **k: sentinel()))
    monkeypatch.setattr(client_mod.RelayClient, "__init__", sentinel)
    monkeypatch.setattr(httpx.Client, "__init__", sentinel)
    monkeypatch.setattr(httpx, "get", sentinel)
    monkeypatch.setattr(socket.socket, "connect", sentinel)
    monkeypatch.setattr(socket, "create_connection", sentinel)
    before = env.relay.budget_reads
    code, lines = compare(env, pilot_root)
    assert code == 0 and lines
    assert env.relay.budget_reads == before and sent(env) == 6  # no relay call of any kind


def test_probe_compare_prints_ttq_ratio_class_and_extremes(env: Env, pilot_root: Path, tmp_path: Path, monkeypatch):
    from costs import cas_class

    monkeypatch.setattr(cas_class, "committed_cas_table", lambda: cas_class.load_cas_table(cas_table_file(tmp_path)))
    env.step("S-3")
    env.step("S-6")
    code, lines = compare(env, pilot_root)
    assert code == 0
    assert lines[0] == "compare probe_date 2026-10-21 symbols 3 saved_hashes verified"
    relind = next(line for line in lines if line.startswith("symbol RELIND bhavcopy_ttq"))
    assert "bhavcopy_ttq 3750 bar_volume_sum_day_window 3750 ratio 1.0000" in relind
    assert "class cas table_version v1" in relind
    assert "U-23" in relind and "pre-open and block-deal" in relind  # the note rides on every ratio line
    nifbee = next(line for line in lines if line.startswith("symbol NIFBEE bhavcopy_ttq"))
    assert "bhavcopy_ttq 7500 bar_volume_sum_day_window 3750 ratio 0.5000" in nifbee
    assert "class session_scope_unknown table_version none" in nifbee  # no row, so no default class
    assert "U-23" in nifbee
    assert "symbol RELIND day_high bhavcopy 101 bars 101 match true" in lines
    assert "symbol RELIND day_low bhavcopy 99 bars 99 match true" in lines
    assert "symbol NIFBEE day_low bhavcopy 98 bars 99 match false" in lines
    assert "symbol BAJFI bhavcopy_row none reason master_unmapped" in lines
    assert any(line.startswith("symbol RELIND sha256 verified") and "after_1540_ist true" in line for line in lines)


def test_probe_compare_with_the_committed_empty_table_reports_scope_unknown(env: Env, pilot_root: Path):
    env.step("S-3")
    env.step("S-6")
    _, lines = compare(env, pilot_root)
    assert all("class session_scope_unknown" in line for line in lines if " bhavcopy_ttq " in line)


def test_probe_compare_rehashes_saved_bytes(env: Env, pilot_root: Path):
    env.step("S-3")
    env.step("S-6")
    answers = env.dir / "answers" / TODAY.isoformat()
    victim = sorted(answers.glob("*-S-6-intraday_RELIND.body"))[0]
    original = victim.read_bytes()
    victim.write_bytes(original[:-1] + b" ")  # one byte changed
    code, lines = compare(env, pilot_root)
    assert code == cli_mod.EXIT_EVIDENCE and lines == ["evidence_error saved_hash_mismatch"]
    victim.write_bytes(original)
    assert compare(env, pilot_root)[0] == 0
    meta_path = victim.with_name(victim.name[: -len(".body")] + ".meta.json")
    meta = json.loads(meta_path.read_text())
    meta["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta))
    assert compare(env, pilot_root)[1] == ["evidence_error saved_hash_mismatch"]
    meta["sha256"] = "not-a-hash"
    meta_path.write_text(json.dumps(meta))
    assert compare(env, pilot_root)[1] == ["evidence_error saved_answer_invalid"]
    meta["sha256"] = sha(original)
    meta_path.write_text(json.dumps(meta))
    victim.unlink()
    assert compare(env, pilot_root)[1] == ["evidence_error saved_body_missing"]


def test_probe_compare_refuses_missing_evidence_and_a_missing_store(env: Env, pilot_root: Path, tmp_path: Path):
    assert compare(env, pilot_root)[1] == ["evidence_error saved_answers_missing"]
    env.step("S-3")  # S-3 answers only, no S-6
    assert compare(env, pilot_root)[1] == ["evidence_error saved_s6_missing"]
    env.step("S-6")
    code, lines = compare(env, tmp_path / "no-such-store")
    assert code == cli_mod.EXIT_EVIDENCE and lines[-1] == "evidence_error pilot_store_store_missing"
    assert not (tmp_path / "no-such-store").exists()  # a missing store is not created


def test_probe_compare_uses_the_highest_seq_s6_answer(env: Env, pilot_root: Path):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [empty_payload()]
    env.step("S-6")  # RELIND first answer empty, retried with the good one
    _, lines = compare(env, pilot_root)
    relind = next(line for line in lines if line.startswith("symbol RELIND bhavcopy_ttq"))
    assert "bar_volume_sum_day_window 3750" in relind


def test_the_cli_documents_exit_code_5_and_both_probe_modes():
    assert cli_mod.EXIT_EVIDENCE == 5
    parser = cli_mod._parser()
    ns = parser.parse_args(["probe-intraday", "compare", "--saved", "a", "--pilot-store", "b"])
    assert (ns.command, ns.probe_mode) == ("probe-intraday", "compare")
    ns = parser.parse_args(["probe-intraday", "step", "--step", "S-3", "--probe-dir", "a"])
    assert (ns.command, ns.probe_mode, ns.step) == ("probe-intraday", "step", "S-3")


def test_relay_api_documents_the_probe_flags_and_every_exit_code():
    text = (Path(__file__).resolve().parents[2] / "gateway" / "RELAY-API.md").read_text()
    exits = text[text.index("- Exit codes:") :]
    for code in ("0 ok", "1 relay error", "2 usage", "3 config error", "4 tunnel error", "5 saved evidence refused"):
        assert code in exits
    assert "INCONCLUSIVE" in exits
    for flag in ("--symbol", "--date", "--probe-dir", "--pilot-store"):
        assert flag in text
    status = text[text.index("Status:") : text.index("## Contract growin-relay/1")]
    for stale in ("arrive on the VM side", "later change"):
        assert stale not in status
    for route in ("budget", "quotes", "preview-order", "security-master", "intraday"):
        assert route in status


def test_probe_a_latched_stop_refuses_a_later_step_and_names_it(env: Env):
    env.step("S-3")
    env.relay.counted += 17
    env.step("S-6")  # a base call refused: the latch is set
    env.relay.counted = START  # a later invocation sees a relay that would allow the call
    code, lines = env.step("S-9")
    assert code == 0 and sent(env) == 3
    assert "slot S-9 intraday RELIND probe_budget_stop" in lines
    assert lines[-1] == "step S-9 status probe_budget_stop sent 3"
    _, closing = env.step("S-12")
    assert closing[0] == f"step S-12 relay_delta 0 ledger_sent 3"


# ------------------------------------------------------- one rule for a slot's outcome (review r3)

_A, _E, _X = "answered", "empty", "error_UPSTREAM_ERROR"


@pytest.mark.parametrize(
    ("attempts", "cut", "expected"),
    [
        ([], False, probe.NOT_RUN),
        ([], True, probe.BUDGET_CUT),
        ([_A], False, probe.ANSWERED),
        ([_E], False, probe.ANSWERED),
        ([_X], False, probe.ERRORED),
        ([_A], True, probe.ANSWERED),  # retry refused: the first answer stays
        ([_E], True, probe.ANSWERED),
        ([_X], True, probe.ERRORED),  # retry refused after an error: no answer exists (codex-r2-1)
        ([_X, _A], False, probe.ANSWERED),
        ([_X, _E], False, probe.ANSWERED),
        ([_X, _X], False, probe.ERRORED),
        ([_A, _A], False, probe.ANSWERED),
        ([_A, _E], False, probe.ANSWERED),
        ([_A, _X], False, probe.ANSWERED),
        ([_E, _A], False, probe.ANSWERED),
        ([_E, _E], False, probe.ANSWERED),
        ([_E, _X], False, probe.ANSWERED),  # empty then 502: the empty answer is saved evidence (codex-r2-2)
        ([_X, _X], True, probe.ERRORED),
        ([_E, _X], True, probe.ANSWERED),
    ],
)
def test_slot_outcome_table(attempts, cut, expected):
    assert probe.slot_outcome(attempts, cut) == expected


def test_slot_outcome_does_not_depend_on_the_order_of_the_tries():
    import itertools

    tries = (_A, _E, _X)
    for first, second in itertools.product(tries, tries):
        for cut in (False, True):
            assert probe.slot_outcome([first, second], cut) == probe.slot_outcome([second, first], cut)


@pytest.mark.parametrize(
    ("outcomes", "results", "verdict"),
    [
        ([probe.ANSWERED], ["answered"], ("complete", 0)),
        ([probe.ANSWERED], [probe.RETRY_SKIPPED], (probe.RETRY_SKIPPED, 0)),
        ([probe.BUDGET_CUT], [probe.BUDGET_STOP], (probe.BUDGET_STOP, 0)),
        ([probe.ANSWERED, probe.BUDGET_CUT], ["answered", probe.BUDGET_STOP], (probe.BUDGET_STOP, 0)),
        ([probe.ERRORED], ["error_X"], (probe.INCONCLUSIVE, 1)),
        ([probe.ERRORED, probe.BUDGET_CUT], ["error_X", probe.BUDGET_STOP], (probe.INCONCLUSIVE, 1)),
        ([probe.BUDGET_CUT, probe.ERRORED], [probe.BUDGET_STOP, "error_X"], (probe.INCONCLUSIVE, 1)),
        ([probe.ERRORED, probe.ANSWERED], ["error_X", probe.RETRY_SKIPPED], (probe.INCONCLUSIVE, 1)),
        ([probe.NOT_RUN], ["not_run"], (probe.INCONCLUSIVE, 1)),
    ],
)
def test_step_verdict_table(outcomes, results, verdict):
    assert probe.step_verdict(outcomes, results) == verdict


def _s9(env: Env, first, second, *, bump: int = 0):
    """Run S-9 (one RELIND slot, one retry allowed) with scripted answers; ``bump`` makes the retry unaffordable."""
    env.step("S-3")
    script = [x for x in (first, second) if x is not None]
    env.relay.answers[("intraday", "RELIND")] = script
    if bump:
        env.relay.bump = {len(env.relay.calls) + 1: bump}
    return env.step("S-9")


def _err():
    return UpstreamError("UPSTREAM_ERROR", http_status=502)


@pytest.mark.parametrize(
    ("first", "second", "retry_fits", "status", "code", "tries"),
    [
        # first try is an answer: only an empty one is retried
        ("ok", None, True, "complete", 0, 1),
        ("ok", None, False, "complete", 0, 1),
        ("empty", "ok", True, "complete", 0, 2),
        ("empty", "empty", True, "complete", 0, 2),
        ("empty", "err", True, "complete", 0, 2),  # codex-r2-2: was INCONCLUSIVE despite saved evidence
        ("empty", None, False, probe.RETRY_SKIPPED, 0, 1),
        # first try is an error
        ("err", "ok", True, "complete", 0, 2),
        ("err", "empty", True, "complete", 0, 2),
        ("err", "err", True, probe.INCONCLUSIVE, 1, 2),
        ("err", None, False, probe.INCONCLUSIVE, 1, 1),
    ],
)
def test_s9_every_combination_of_two_tries_and_a_budget_refusal(env: Env, first, second, retry_fits, status, code, tries):
    make = {"ok": lambda: None, "empty": lambda: empty_payload(), "err": _err}
    first_try = make[first]() if first != "ok" else make_payload(body_of(minute_rows()))
    second_try = None
    if second is not None:
        second_try = make[second]() if second != "ok" else make_payload(body_of(minute_rows()))
    got, lines = _s9(env, first_try, second_try, bump=0 if retry_fits else 13)
    assert (got != 0) == (code != 0)
    assert lines[-1].startswith(f"step S-9 status {status} ")
    assert sent(env) == 3 + tries


def test_a_budget_cut_after_an_error_keeps_the_step_inconclusive_when_later_slots_stop(env: Env):
    """codex-r2-1: S-6 RELIND 502 at relay delta 19, its retry refused, BAJFI fits, NIFBEE is cut."""
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [_err()]
    env.relay.bump = {len(env.relay.calls) + 1: 15}
    code, lines = env.step("S-6")
    assert code == cli_mod.EXIT_RELAY != 0
    assert "slot S-6 intraday RELIND probe_retry_skipped" in lines
    assert "slot S-6 intraday NIFBEE probe_budget_stop" in lines
    assert lines[-1].startswith("step S-6 status INCONCLUSIVE ")
    assert env.ledger()["stopped"] is True


def test_every_later_slot_cut_after_an_early_error_still_exits_nonzero(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [_err()]
    env.relay.bump = {len(env.relay.calls) + 1: 16}  # delta 20 after the send: every later base call is cut
    code, lines = env.step("S-6")
    assert code != 0 and lines[-1].startswith("step S-6 status INCONCLUSIVE ")
    assert sum(1 for line in lines if line.endswith("probe_budget_stop")) == 2


def test_a_pure_budget_cut_still_exits_zero_with_its_own_status(env: Env):
    env.step("S-3")
    env.relay.counted += 17
    code, lines = env.step("S-6")
    assert code == 0 and lines[-1].startswith("step S-6 status probe_budget_stop ")


def test_a_repeated_step_whose_earlier_run_errored_and_now_answers_is_complete(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "RELIND")] = [_err(), _err()]
    assert env.step("S-9")[0] != 0
    code, lines = env.step("S-9")
    assert code == 0 and lines[-1].startswith("step S-9 status complete ")


# ------------------------------- evidence is scoped to (step, symbol, date), quota to the slot (review r4)


def test_s7_a_answered_then_b_with_two_502s_is_inconclusive(env: Env):
    """codex-r3: B's two 502s were hidden by A's persisted answer and the step exited 0."""
    env.step("S-3")
    assert env.step("S-7", "--symbol", "AAA")[0] == 0
    env.relay.answers[("intraday", "BBB")] = [_err(), _err()]
    code, lines = env.step("S-7", "--symbol", "BBB")
    assert code == cli_mod.EXIT_RELAY != 0
    assert lines[-1].startswith("step S-7 status INCONCLUSIVE ")
    assert sent(env) == 3 + 1 + 2


def test_s7_b_with_two_502s_then_a_answered_is_complete_for_a(env: Env):
    env.step("S-3")
    env.relay.answers[("intraday", "BBB")] = [_err(), _err()]
    assert env.step("S-7", "--symbol", "BBB")[0] != 0
    code, lines = env.step("S-7", "--symbol", "AAA")
    assert code == 0 and lines[-1].startswith("step S-7 status complete ")  # B's errors do not taint A


def test_s7_an_empty_answer_for_b_is_still_valid_evidence_for_b(env: Env):
    env.step("S-3")
    env.step("S-7", "--symbol", "AAA")
    env.relay.answers[("intraday", "BBB")] = [empty_payload("BBB"), _err()]
    code, lines = env.step("S-7", "--symbol", "BBB")
    assert code == 0 and lines[-1].startswith("step S-7 status complete ")


def test_s7_two_symbols_still_share_one_quota_slot(env: Env):
    env.step("S-3")
    env.step("S-7", "--symbol", "AAA")
    env.step("S-7", "--symbol", "BBB")
    slots = env.ledger()["steps"]["S-7"]["slots"]
    assert list(slots) == ["intraday-s7"] and slots["intraday-s7"]["requests"] == 2
    assert sorted(slots["intraday-s7"]["evidence"]) == [f"AAA@{TODAY.isoformat()}", f"BBB@{TODAY.isoformat()}"]
    assert probe.Ledger.load(env.dir, TODAY).unsent_base_slots() == probe.BASE_CALLS - 3 - 1


def test_s7_a_refused_retry_for_b_is_a_budget_cut_for_b_not_a_pass_from_a(env: Env):
    env.step("S-3")
    env.step("S-7", "--symbol", "AAA")
    env.relay.counted += 12  # B is a retry-kind call that no longer fits
    code, lines = env.step("S-7", "--symbol", "BBB")
    assert "slot S-7 intraday BBB probe_retry_skipped" in lines
    assert lines[-1].startswith("step S-7 status probe_budget_stop ") and code == 0


def test_s8_a_date_answered_then_another_date_with_a_502_is_inconclusive(env: Env):
    env.step("S-3")
    other = HOLIDAY.replace(day=HOLIDAY.day - 1)
    assert env.step("S-8", "--date", HOLIDAY.isoformat())[0] == 0
    env.relay.answers[("intraday", "RELIND")] = [_err()]
    code, lines = env.step("S-8", "--date", other.isoformat())
    assert code != 0 and lines[-1].startswith("step S-8 status INCONCLUSIVE ")
    slots = env.ledger()["steps"]["S-8"]["slots"]
    assert len(slots) == 1 and next(iter(slots.values()))["requests"] == 2  # one quota slot, two dates


def test_s8_a_date_with_a_502_then_another_date_answered_is_complete(env: Env):
    env.step("S-3")
    other = HOLIDAY.replace(day=HOLIDAY.day - 1)
    env.relay.answers[("intraday", "RELIND")] = [_err()]
    assert env.step("S-8", "--date", HOLIDAY.isoformat())[0] != 0
    code, lines = env.step("S-8", "--date", other.isoformat())
    assert code == 0 and lines[-1].startswith("step S-8 status complete ")
