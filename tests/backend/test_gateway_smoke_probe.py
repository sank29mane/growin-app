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
