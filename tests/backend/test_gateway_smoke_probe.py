"""Smoke probe tests against a fake transport. No network, no real credentials.

Fake secrets are generated at runtime. The AppKey deliberately contains the
characters ^ = # to exercise URL-encoding in the redaction check.
"""

from __future__ import annotations

import base64
import json
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import smoke_probe as sp  # noqa: E402
from gateway_vm.breeze_client import V1_BASE, BreezeClient, compact_json  # noqa: E402
from gateway_vm.egress import METADATA_URL, EgressChecker  # noqa: E402
from gateway_vm.transport import HttpResponse, TransportError  # noqa: E402

EXPECTED_IP = "203.0.113.7"
ECHO = "https://api.ipify.org"


class FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def monotonic(self) -> float:
        self._t += 0.001
        return self._t

    def now_utc(self) -> datetime:
        return datetime(2026, 10, 1, 10, 23, 56, 400000, tzinfo=timezone.utc)


class FakeTransport:
    """Records (method, url, headers, body); answers by URL prefix."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        self.requests.append((method, url, dict(headers), body))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(url, body) if callable(answer) else answer
        raise TransportError("host_not_allowed", "fake")


def ok(text: str) -> HttpResponse:
    return HttpResponse(200, {}, text.encode())


class Secrets:
    def __init__(self) -> None:
        self.app_key = "AK" + secrets.token_urlsafe(8) + "^=#"
        self.secret_key = secrets.token_urlsafe(16)
        self.userid = "U" + secrets.token_hex(3).upper()
        self.code = "A1" + secrets.token_hex(6)
        digits = "".join(secrets.choice("0123456789") for _ in range(8))
        self.token = base64.b64encode(f"{self.userid}:{digits}".encode()).decode()

    def values(self) -> list[str]:
        return [self.app_key, self.secret_key, self.userid, self.code, self.token]

    def customer_body(self, userid: str | None = None) -> str:
        return json.dumps(
            {
                "Success": {
                    "exg_trade_date": {"NSE": "01-Oct-2026"},
                    "exg_status": {"NSE": "O"},
                    "segments_allowed": {
                        "Trading": "Y", "Equity": "Y", "Derivatives": "Z", "Currency": "Z",
                    },
                    "idirect_userid": userid or self.userid,
                    "session_token": self.token,
                    "idirect_user_name": "FAKE NAME",
                    "idirect_lastlogin_time": "01-Oct-2026 15:50:00",
                },
                "Status": 200,
                "Error": None,
            }
        )


def prompts_for(s: Secrets) -> sp.Prompts:
    return sp.Prompts(
        app_key=lambda: s.app_key,
        secret_key=lambda: s.secret_key,
        expected_userid=lambda: s.userid,
        expected_ip=lambda: EXPECTED_IP,
        api_session=lambda: s.code,
    )


def build(s: Secrets, *, echo_ip: str = EXPECTED_IP, customer: str | None = None):
    routes = {
        METADATA_URL: ok(EXPECTED_IP),
        ECHO: ok(echo_ip),
        V1_BASE + "customerdetails": ok(customer if customer is not None else s.customer_body()),
    }
    transport = FakeTransport(routes)
    clock = FakeClock()
    client = BreezeClient(transport, clock=clock)

    def egress_factory(expected_ip: str) -> EgressChecker:
        return EgressChecker(transport, expected_ip=expected_ip, echo_url=ECHO, clock=clock)

    return transport, client, egress_factory


def run(s: Secrets, tmp_path: Path, transport_bits, steps=("S0", "S1")):
    transport, client, egress_factory = transport_bits
    sleeps: list[float] = []
    result = sp.run_probe(
        client=client,
        egress_factory=egress_factory,
        prompts=prompts_for(s),
        plan=sp.ProbePlan(),
        out_dir=tmp_path / "out",
        sleep=sleeps.append,
        steps=steps,
    )
    return result, sleeps


# ------------------------------------------------------------------ tracer


def test_happy_path_writes_one_redacted_0600_file(tmp_path):
    s = Secrets()
    bits = build(s)
    result, _ = run(s, tmp_path, bits)

    path = tmp_path / "out" / sp.RESULT_FILENAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == [sp.RESULT_FILENAME]

    on_disk = json.loads(path.read_text())
    assert on_disk["schema"] == "growin.smoke.breeze.v1"
    assert on_disk["s0_egress"] == {"ok": True, "metadata_match": True, "echo_match": True}
    s1 = on_disk["s1_customer_details"]
    assert s1["pass"] is True
    assert s1["userid_matches"] is True
    assert s1["code_charset"] == "alnum"
    assert s1["code_length"] == len(s.code)
    assert s1["token_b64_pair"] is True
    assert s1["status_value"] == 200 and s1["status_type"] == "int"
    assert s1["segments"] == {"Trading": "Y", "Equity": "Y", "Derivatives": "Z", "Currency": "Z"}
    assert s1["lastlogin_tz_hint"] == "IST"
    assert "idirect_userid" in s1["success_keys"]
    assert result["calls"] == {"breeze": 1, "max": 20}


def test_request_order_and_customerdetails_shape(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build(s)
    run(s, tmp_path, (transport, client, egress_factory))

    urls = [r[1] for r in transport.requests]
    assert urls == [METADATA_URL, ECHO, V1_BASE + "customerdetails"]
    method, _url, headers, body = transport.requests[2]
    assert method == "GET"
    assert body == compact_json({"SessionToken": s.code, "AppKey": s.app_key}).encode()
    assert "x-checksum" not in {k.lower() for k in headers}


def test_serialized_result_contains_no_secret_in_any_form(tmp_path):
    s = Secrets()
    run(s, tmp_path, build(s))
    text = (tmp_path / "out" / sp.RESULT_FILENAME).read_text()
    for value in s.values() + [EXPECTED_IP]:
        for form in (
            value,
            quote_plus(value),
            base64.b64encode(value.encode()).decode(),
        ):
            assert form not in text


def test_egress_mismatch_sends_no_breeze_request(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build(s, echo_ip="198.51.100.9")
    result, _ = run(s, tmp_path, (transport, client, egress_factory))

    assert all("icicidirect" not in r[1] for r in transport.requests)
    assert client.calls == 0
    on_disk = json.loads((tmp_path / "out" / sp.RESULT_FILENAME).read_text())
    assert on_disk["s0_egress"]["ok"] is False
    assert "s1_customer_details" not in on_disk
    assert result["s0_egress"]["echo_match"] is False


def test_status_5_body_fails_s1_and_stops(tmp_path):
    s = Secrets()
    body = json.dumps({"Success": None, "Status": 5, "Error": "Public Key does not exist."})
    result, _ = run(s, tmp_path, build(s, customer=body), steps=sp.ALL_STEPS)

    s1 = result["s1_customer_details"]
    assert s1["pass"] is False
    assert s1["error_class"] == "public_key_missing"
    assert s1["status_value"] == 5
    assert s1["userid_matches"] is False
    assert "s2_bars" not in result
    assert result["calls"]["breeze"] == 1


def test_userid_mismatch_fails_s1(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build(s, customer=s.customer_body(userid="OTHER1")))
    assert result["s1_customer_details"]["pass"] is False
    assert result["s1_customer_details"]["userid_matches"] is False


def test_existing_result_is_not_overwritten(tmp_path):
    s = Secrets()
    run(s, tmp_path, build(s))
    with pytest.raises(FileExistsError):
        run(s, tmp_path, build(s))


def test_redaction_self_check_blocks_the_write(tmp_path):
    s = Secrets()
    # A server that echoes the session code back in the Status field, which the
    # probe records as status_value when it is a string.
    leaky = json.dumps({"Success": None, "Status": s.code, "Error": "x"})
    transport, client, egress_factory = build(s, customer=leaky)
    with pytest.raises(sp.RedactionError):
        run(s, tmp_path, (transport, client, egress_factory))
    assert not (tmp_path / "out" / sp.RESULT_FILENAME).exists()


# -------------------------------------------------------------------- main


def _patch_main(monkeypatch, s: Secrets, bits, tty: bool):
    class Stdin:
        def isatty(self):
            return tty

    monkeypatch.setattr(sys, "stdin", Stdin())
    transport, client, egress_factory = bits
    monkeypatch.setattr(sp, "_build_runtime", lambda: (client, egress_factory, lambda _s: None))
    monkeypatch.setattr(sp, "default_prompts", lambda: prompts_for(s))


def test_main_refuses_without_tty_and_never_prompts(monkeypatch, tmp_path):
    s = Secrets()
    _patch_main(monkeypatch, s, build(s), tty=False)

    def boom():
        raise AssertionError("prompted without a TTY")

    monkeypatch.setattr(sp, "default_prompts", boom)
    assert sp.main(["--out", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()


def test_main_exit_3_on_egress_mismatch(monkeypatch, tmp_path):
    s = Secrets()
    _patch_main(monkeypatch, s, build(s, echo_ip="198.51.100.9"), tty=True)
    assert sp.main(["--out", str(tmp_path / "out")]) == 3


def test_main_exit_5_when_s1_fails(monkeypatch, tmp_path):
    s = Secrets()
    body = json.dumps({"Success": None, "Status": 5, "Error": "Invalid session."})
    _patch_main(monkeypatch, s, build(s, customer=body), tty=True)
    assert sp.main(["--out", str(tmp_path / "out")]) == 5


def test_main_output_is_value_free(monkeypatch, tmp_path, capsys):
    s = Secrets()
    _patch_main(monkeypatch, s, build(s), tty=True)
    sp.main(["--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    for value in s.values() + [EXPECTED_IP]:
        assert value not in out


def test_main_validate_needs_no_tty(monkeypatch, tmp_path):
    class Stdin:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Stdin())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "wrong"}))
    assert sp.main(["--validate", str(bad)]) == 1


# =============================================================== S2 to S5
# Full-run tests: canned responses built from the documented key sets.

from datetime import date, timedelta  # noqa: E402

from gateway_vm.breeze_client import V2_HISTORICAL_URL  # noqa: E402


def _bars_response(closes_by_date: dict[str, float], stamp: str = "{d} 00:00:00") -> HttpResponse:
    rows = [
        {
            "close": c, "datetime": stamp.format(d=d), "exchange_code": "NSE",
            "high": c + 1, "low": c - 1, "open": c, "stock_code": "X", "volume": 1000,
        }
        for d, c in closes_by_date.items()
    ]
    return ok(json.dumps({"Success": rows, "Status": 200, "Error": None}))


def _quote_fields(ltp, bid, offer, up, lo, prev):
    return {
        "ltp": ltp, "ltt": "Thu Oct 01 2026 15:29:58", "best_bid_price": bid,
        "best_offer_price": offer, "upper_circuit": up, "lower_circuit": lo,
        "previous_close": prev, "open": ltp, "high": ltp, "low": ltp,
    }


QUOTES = {
    "RELIND": _quote_fields(1402.30, 1402.20, 1402.40, 1542.50, 1262.10, 1400.00),
    "BAJFI": _quote_fields(9300.50, 9300.00, 9301.00, 10230.50, 8370.50, 9290.00),
    "NIFBEE": _quote_fields(280.35, 280.30, 280.40, 308.35, 252.35, 280.00),
}
BAJFI_DAYS = [
    "2025-06-09", "2025-06-10", "2025-06-11", "2025-06-12", "2025-06-13",
    "2025-06-16", "2025-06-17", "2025-06-18", "2025-06-19", "2025-06-20",
]
RELIND_DAYS = [
    "2024-10-21", "2024-10-22", "2024-10-23", "2024-10-24", "2024-10-25",
    "2024-10-28", "2024-10-29", "2024-10-30", "2024-10-31",
]


def full_routes(s: Secrets, *, bajfi_after: float = 940.0, second_token: str | None = None,
                bars_ok: bool = True, interval_ok: tuple[str, ...] = ("1day", "day"),
                quotes_ok: bool = True):
    state = {"customer_calls": 0}

    def customer(url, body):
        state["customer_calls"] += 1
        text = s.customer_body()
        if state["customer_calls"] > 1 and second_token:
            text = text.replace(s.token, second_token)
        return ok(text)

    def v2(url, body):
        q = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
        if not bars_ok or q["interval"] not in interval_ok:
            return ok(json.dumps({"Success": None, "Status": 500, "Error": "Invalid interval."}))
        if q["stock_code"] == "BAJFI":
            closes = {d: (9400.0 if d < "2025-06-16" else bajfi_after) for d in BAJFI_DAYS}
            return _bars_response(closes)
        if q["from_date"].startswith("2024-10-21"):
            closes = {d: (2900.0 if d < "2024-10-28" else 1450.0) for d in RELIND_DAYS}
            return _bars_response(closes)
        start = date(2021, 10, 1)
        closes = {(start + timedelta(days=i)).isoformat(): 1000.0 + i for i in range(1000)}
        return _bars_response(closes)

    def quotes(url, body):
        code = json.loads(body)["stock_code"]
        if not quotes_ok:
            return ok(json.dumps({"Success": None, "Status": 500, "Error": "No Data Found"}))
        return ok(json.dumps({"Success": [QUOTES[code]], "Status": 200, "Error": None}))

    def preview(url, body):
        return ok(json.dumps({
            "Success": {"brokerage": 7.5, "stt": 1.4, "gst": "1.35", "note": "calculator"},
            "Status": 200, "Error": None,
        }))

    return {
        METADATA_URL: ok(EXPECTED_IP),
        ECHO: ok(EXPECTED_IP),
        V1_BASE + "customerdetails": customer,
        V2_HISTORICAL_URL: v2,
        V1_BASE + "quotes": quotes,
        V1_BASE + "preview_order": preview,
    }


def build_full(s: Secrets, **kwargs):
    transport = FakeTransport(full_routes(s, **kwargs))
    clock = FakeClock()
    client = BreezeClient(transport, clock=clock)

    def egress_factory(expected_ip: str) -> EgressChecker:
        return EgressChecker(transport, expected_ip=expected_ip, echo_url=ECHO, clock=clock)

    return transport, client, egress_factory


def breeze_urls(transport: FakeTransport) -> list[str]:
    return [r[1] for r in transport.requests if "icicidirect" in r[1]]


def test_full_run_order_cap_and_pacing(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build_full(s)
    result, sleeps = run(s, tmp_path, (transport, client, egress_factory), steps=sp.ALL_STEPS)

    urls = breeze_urls(transport)
    kinds = []
    for url in urls:
        kinds.append(
            "cust" if url.endswith("customerdetails")
            else "bars" if url.startswith(V2_HISTORICAL_URL)
            else "quote" if url.endswith("quotes")
            else "preview"
        )
    assert kinds == ["cust"] + ["bars"] * 3 + ["quote"] * 3 + ["preview"] * 4 + ["cust", "quote"]
    assert client.calls == len(urls) == 13 <= 20
    assert result["calls"] == {"breeze": 13, "max": 20}
    assert sleeps == [1.0] * 12
    # S0 ran before any Breeze URL
    assert [r[1] for r in transport.requests][:2] == [METADATA_URL, ECHO]
    # S5 is the last Breeze call
    assert kinds[-1] == "quote"
    assert result["s2_interval_wire"] == "1day"
    assert sp.validate_result(result) == []


def test_s2_adjustment_raw_and_formats(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s, bajfi_after=940.0), steps=sp.ALL_STEPS)
    a, b, c = result["s2_bars"][:3]
    assert [a["id"], b["id"], c["id"]] == ["S2a", "S2b", "S2c"]
    assert a["adjustment_reading"] == "raw"
    assert a["pre_ex_close"] == 9400.0 and a["ex_close"] == 940.0
    assert a["close_ratio"] == 0.1
    assert a["datetime_format"] == "YYYY-MM-DD HH:MM:SS"
    assert a["time_of_day"] == ["00:00:00"]
    assert a["first_date"] == "2025-06-09" and a["last_date"] == "2025-06-20"
    assert a["boundary_first_included"] is True and a["boundary_last_included"] is True
    assert a["upstream_from"] == "2025-06-09T00:00:00.000Z"
    assert a["upstream_to"] == "2025-06-20T23:59:59.000Z"
    assert a["row_keys"] == [
        "close", "datetime", "exchange_code", "high", "low", "open", "stock_code", "volume",
    ]
    assert a["value_types"]["close"] == "float"
    assert b["adjustment_reading"] == "raw"  # 0.5 ratio against expected 0.5
    assert c["adjustment_reading"] == "n/a"
    assert c["rows"] == 1000 and c["capped_at_1000"] is True


def test_s2_adjusted_reading(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s, bajfi_after=9390.0), steps=sp.ALL_STEPS)
    assert result["s2_bars"][0]["adjustment_reading"] == "adjusted"


def test_s2d_retries_with_day_only_when_all_fail(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build_full(s, interval_ok=("day",))
    result, _ = run(s, tmp_path, (transport, client, egress_factory), steps=sp.ALL_STEPS)
    assert [i["id"] for i in result["s2_bars"]] == ["S2a", "S2b", "S2c", "S2d"]
    assert result["s2_interval_wire"] == "day"
    assert result["s2_bars"][0]["error_class"] == "other"
    assert result["s2_bars"][0]["error_text"] == "Invalid interval."
    assert result["s2_bars"][3]["rows"] == 10
    assert "interval=day" in breeze_urls(transport)[4]


def test_s2_wire_none_when_nothing_returns_rows(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s, bars_ok=False), steps=sp.ALL_STEPS)
    assert result["s2_interval_wire"] == "none"
    # later steps still ran
    assert len(result["s3_quotes"]) == 3


def test_s3_quote_fields_and_tick_inference(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s), steps=sp.ALL_STEPS)
    rel, baj, nif = result["s3_quotes"]
    assert [q["stock_code"] for q in result["s3_quotes"]] == ["RELIND", "BAJFI", "NIFBEE"]
    assert rel["has_circuits"] is True
    assert rel["inferred_tick_paise"] == 10 and rel["nse_table_tick_paise"] == 10
    assert baj["inferred_tick_paise"] == 50 and baj["nse_table_tick_paise"] == 50
    assert nif["inferred_tick_paise"] == 5 and nif["nse_table_tick_paise"] == 5
    assert rel["ltt"] == "Thu Oct 01 2026 15:29:58"
    assert "ltp" in rel["keys"]


def test_s4_preview_four_calls_price_floored_to_tick(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build_full(s)
    result, _ = run(s, tmp_path, (transport, client, egress_factory), steps=sp.ALL_STEPS)
    previews = result["s4_preview"]
    assert [(p["action"], p["quantity"]) for p in previews] == [
        ("buy", 1), ("buy", 35), ("sell", 1), ("sell", 35),
    ]
    assert {p["price"] for p in previews} == {"1402.30"}
    assert previews[0]["lines"] == {"brokerage": 7.5, "stt": 1.4, "gst": 1.35}
    sent = [json.loads(r[3]) for r in transport.requests if r[1].endswith("preview_order")]
    assert [b["quantity"] for b in sent] == ["1", "35", "1", "35"]
    assert all(b["order_type"] == "limit" and b["product"] == "cash" for b in sent)


def test_s4_skipped_when_quotes_fail(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build_full(s, quotes_ok=False)
    result, _ = run(s, tmp_path, (transport, client, egress_factory), steps=sp.ALL_STEPS)
    assert result["s4_preview"] == []
    assert "s4_skipped_no_ltp" in result["notes"]
    assert result["s3_quotes"][0]["error_class"] == "no_data"
    assert not any(u.endswith("preview_order") for u in breeze_urls(transport))


def test_s5_replay_records_token_reuse(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s), steps=sp.ALL_STEPS)
    assert result["s5_replay"] == {
        "second_exchange": "success", "error_class": None,
        "tokens_equal": True, "first_token_still_works": True,
    }
    s2 = Secrets()
    other = base64.b64encode(f"{s2.userid}:99999999".encode()).decode()
    result, _ = run(s2, tmp_path / "second", build_full(s2, second_token=other), steps=sp.ALL_STEPS)
    assert result["s5_replay"]["tokens_equal"] is False
    text = (tmp_path / "second" / "out" / sp.RESULT_FILENAME).read_text()
    assert other not in text  # the second token is redacted too


def test_full_run_file_has_no_secret_in_any_form(tmp_path):
    s = Secrets()
    run(s, tmp_path, build_full(s), steps=sp.ALL_STEPS)
    text = (tmp_path / "out" / sp.RESULT_FILENAME).read_text()
    for value in s.values() + [EXPECTED_IP]:
        for form in (value, quote_plus(value), base64.b64encode(value.encode()).decode()):
            assert form not in text


def test_call_cap_stops_before_twenty(tmp_path):
    s = Secrets()
    transport, client, egress_factory = build_full(s)
    client.calls = 17  # pretend earlier calls already happened
    result, _ = run(s, tmp_path, (transport, client, egress_factory), steps=sp.ALL_STEPS)
    assert client.calls == 20
    assert "call_cap_reached" in result["notes"]
    assert result["calls"]["breeze"] == 20


def test_step_exception_is_recorded_and_run_continues(tmp_path, monkeypatch):
    s = Secrets()

    def boom(st, result):
        raise RuntimeError("secret-looking detail must not be recorded")

    monkeypatch.setattr(sp, "_step_s2", boom)
    result, _ = run(s, tmp_path, build_full(s), steps=sp.ALL_STEPS)
    assert "s2_exception" in result["notes"]
    assert "s3_quotes" in result
    assert "secret-looking" not in json.dumps(result)


# ------------------------------------------------------------ helper units


def test_nse_tick_table_edges():
    assert sp.nse_table_tick_paise(249.99) == 1
    assert sp.nse_table_tick_paise(250) == 5
    assert sp.nse_table_tick_paise(999.95) == 5
    assert sp.nse_table_tick_paise(1000) == 10
    assert sp.nse_table_tick_paise(1402.30) == 10
    assert sp.nse_table_tick_paise(5000) == 50
    assert sp.nse_table_tick_paise(10000) == 100
    assert sp.nse_table_tick_paise(20000) == 500
    assert sp.nse_table_tick_paise(None) is None


def test_tick_gcd_paise():
    assert sp.tick_gcd_paise([1402.30, 1402.20, 1402.40, 1542.50, 1262.10]) == 10
    assert sp.tick_gcd_paise([None, 0, 5.0]) == 500
    assert sp.tick_gcd_paise([None, 0]) is None


def test_window_flag_parsing():
    import argparse

    w = sp._parse_window("2025-06-09:2025-06-20:2025-06-16", "BAJFI")
    assert (w.from_date, w.to_date, w.ex_date) == ("2025-06-09", "2025-06-20", "2025-06-16")
    assert sp._parse_window("2025-06-09:2025-06-20", "BAJFI").ex_date is None
    with pytest.raises(argparse.ArgumentTypeError):
        sp._parse_window("2025-06-09", "BAJFI")
    with pytest.raises(argparse.ArgumentTypeError):
        sp._parse_window("09-06-2025:2025-06-20", "BAJFI")


# --------------------------------------------------------- validate_result


@pytest.fixture
def full_result(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build_full(s), steps=sp.ALL_STEPS)
    return json.loads(json.dumps(result))


def test_validate_accepts_full_result(full_result):
    assert sp.validate_result(full_result) == []


def test_validate_rejects_missing_keys(full_result):
    del full_result["s5_replay"]
    assert any("s5_replay" in p for p in sp.validate_result(full_result))
    full_result["s5_replay"] = {"second_exchange": "success"}
    assert any("missing key" in p for p in sp.validate_result(full_result))


def test_validate_rejects_unexpected_key(full_result):
    full_result["s1_customer_details"]["extra"] = "x"
    assert any("unexpected key" in p for p in sp.validate_result(full_result))


def test_validate_rejects_partial_egress_failure_file(tmp_path):
    s = Secrets()
    result, _ = run(s, tmp_path, build(s, echo_ip="198.51.100.9"))
    assert sp.validate_result(result)  # an egress-failed run is not a usable smoke result


def test_validate_rejects_secret_shaped_strings(full_result):
    import hashlib

    full_result["notes"] = [hashlib.sha256(b"x").hexdigest()]
    assert any("secret-shaped" in p for p in sp.validate_result(full_result))
    full_result["notes"] = [base64.b64encode(b"FAKEUSER:12345678").decode()]
    problems = sp.validate_result(full_result)
    assert any("secret-shaped" in p for p in problems)
    assert not any("FAKEUSER" in p for p in problems)  # problems never echo values


def test_main_validate_ok_on_full_result(monkeypatch, tmp_path, capsys, full_result):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(full_result))

    class Stdin:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", Stdin())
    capsys.readouterr()  # drop output printed while the fixture ran the probe
    assert sp.main(["--validate", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "OK"
