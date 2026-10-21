"""relay.json loader refusals and the smoke-driven client settings (61-05).

Files are written under tmp_path with an explicit chmod. No network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import breeze_client as bc  # noqa: E402
from gateway_vm.config import ConfigError, RelayConfig, load_relay_config  # noqa: E402
from gateway_vm.transport import HttpResponse  # noqa: E402

GOOD = {
    "expected_egress_ip": "203.0.113.7",
    "expected_userid": "FAKEUSER01",
    "secret_names": {"app_key": "breeze-api-key", "secret_key": "breeze-api-secret"},
}


def write(tmp_path: Path, text: str | dict, mode: int = 0o600, name: str = "relay.json") -> Path:
    path = tmp_path / name
    path.write_text(text if isinstance(text, str) else json.dumps(text))
    os.chmod(path, mode)
    return path


def good(**changes) -> dict:
    data = json.loads(json.dumps(GOOD))
    data.update(changes)
    return data


def refused(tmp_path: Path, text, code: str, key: str | None = None, mode: int = 0o600) -> ConfigError:
    with pytest.raises(ConfigError) as err:
        load_relay_config(write(tmp_path, text, mode))
    assert err.value.code == code, (err.value.code, err.value.key)
    if key is not None:
        assert err.value.key == key
    return err.value


# ------------------------------------------------------------- accepted


def test_a_good_file_loads_with_the_documented_defaults(tmp_path):
    cfg = load_relay_config(write(tmp_path, GOOD))
    assert cfg.expected_egress_ip == "203.0.113.7"
    assert cfg.expected_userid == "FAKEUSER01"
    assert dict(cfg.secret_names) == {"app_key": "breeze-api-key", "secret_key": "breeze-api-secret"}
    assert cfg.egress_echo_url == "https://api.ipify.org"
    assert cfg.security_master_url == (
        "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
    )
    assert (cfg.per_minute, cfg.per_day, cfg.security_master_per_hour) == (80, 4500, 6)


def test_limits_at_the_ceiling_are_accepted(tmp_path):
    cfg = load_relay_config(write(tmp_path, good(per_minute=100, per_day=5000, security_master_per_hour=3)))
    assert (cfg.per_minute, cfg.per_day, cfg.security_master_per_hour) == (100, 5000, 3)


def test_a_looser_mode_is_fine_inside_a_systemd_credentials_dir(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    path = write(creds, GOOD, mode=0o644)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    assert load_relay_config(path).expected_userid == "FAKEUSER01"


def test_the_credentials_dir_exemption_is_only_for_files_inside_it(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    refused(tmp_path, GOOD, "file_mode", mode=0o644)


# -------------------------------------------------------------- refusals


@pytest.mark.parametrize("key", ["expected_egress_ip", "expected_userid", "secret_names"])
def test_missing_required_key(tmp_path, key):
    data = good()
    del data[key]
    refused(tmp_path, data, "missing_key", key)


@pytest.mark.parametrize("key", ["app_key", "secret_key"])
def test_missing_secret_name(tmp_path, key):
    data = good()
    del data["secret_names"][key]
    refused(tmp_path, data, "missing_key", f"secret_names.{key}")


def test_unknown_keys_at_every_level(tmp_path):
    refused(tmp_path, good(extra_setting=1), "unknown_key", "extra_setting")
    data = good()
    data["secret_names"]["api_secret_value"] = "breeze-x"
    refused(tmp_path, data, "unknown_key", "secret_names.api_secret_value")


@pytest.mark.parametrize("ip", ["2001:db8::1", "203.0.113", "not-an-ip", "203.0.113.7/32", "", 7, None])
def test_expected_ip_must_be_ipv4(tmp_path, ip):
    refused(tmp_path, good(expected_egress_ip=ip), "bad_ip", "expected_egress_ip")


@pytest.mark.parametrize("userid", ["ab", "x" * 33, "FAKE USER", "FAKE-USER", "FAKEéUSER", "", 5])
def test_userid_pattern(tmp_path, userid):
    refused(tmp_path, good(expected_userid=userid), "bad_userid", "expected_userid")


@pytest.mark.parametrize(
    "mutate, key",
    [
        (lambda d: d.update(expected_egress_ip="<EXPECTED_EGRESS_IP>"), "expected_egress_ip"),
        (lambda d: d.update(expected_userid="<BROKER_USER_ID>"), "expected_userid"),
        (lambda d: d["secret_names"].update(app_key="<APP_KEY_SECRET_NAME>"), "secret_names.app_key"),
        (lambda d: d.update(egress_echo_url="<ECHO_URL>"), "egress_echo_url"),
    ],
)
def test_unfilled_placeholders_are_refused(tmp_path, mutate, key):
    data = good()
    mutate(data)
    refused(tmp_path, data, "placeholder", key)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.ipify.org",
        "https://evil.example/ip",
        "https://api.ipify.org.evil.example",
        "https://user:pw@api.ipify.org",
        "https://api.ipify.org:8443",
        "ftp://api.ipify.org",
        "api.ipify.org",
        "",
        5,
    ],
)
@pytest.mark.parametrize("key", ["egress_echo_url", "security_master_url"])
def test_urls_must_be_https_on_the_transport_allowlist(tmp_path, key, url):
    refused(tmp_path, good(**{key: url}), "bad_url", key)


@pytest.mark.parametrize(
    "key, value, code",
    [
        ("per_minute", 101, "limit_too_high"),
        ("per_day", 5001, "limit_too_high"),
        ("per_minute", 0, "bad_limit"),
        ("per_day", -1, "bad_limit"),
        ("per_minute", "80", "bad_limit"),
        ("per_minute", 80.0, "bad_limit"),
        ("per_day", True, "bad_limit"),
        ("per_day", None, "bad_limit"),
        ("security_master_per_hour", 0, "bad_limit"),
    ],
)
def test_limits(tmp_path, key, value, code):
    refused(tmp_path, good(**{key: value}), code)


def test_duplicate_json_keys_are_refused_at_any_depth(tmp_path):
    top = '{"expected_egress_ip": "203.0.113.7", "expected_egress_ip": "203.0.113.8", ' + json.dumps(GOOD)[1:]
    refused(tmp_path, top, "duplicate_key", "expected_egress_ip")
    nested = (
        '{"expected_egress_ip": "203.0.113.7", "expected_userid": "FAKEUSER01", '
        '"secret_names": {"app_key": "a-1", "app_key": "a-2", "secret_key": "b-1"}}'
    )
    refused(tmp_path, nested, "duplicate_key", "app_key")


@pytest.mark.parametrize("text", ["", "{", "[]", "null", '"x"', '{"per_minute": NaN}', "\xff"])
def test_not_a_json_object(tmp_path, text):
    with pytest.raises(ConfigError):
        load_relay_config(write(tmp_path, text))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_relay_config(tmp_path / "absent.json")
    assert err.value.code == "unreadable"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o700, 0o601])
def test_file_mode_looser_than_0600_is_refused(tmp_path, mode):
    refused(tmp_path, GOOD, "file_mode", mode=mode)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_file_mode_0600_and_stricter_are_accepted(tmp_path, mode):
    load_relay_config(write(tmp_path, GOOD, mode))


# ------------------------------------------------------- no value leaks


SENTINELS = ["SENTINELUSER99", "198.51.100.77", "leaky-secret-name-5"]


def test_error_messages_never_contain_a_value(tmp_path):
    cases = [
        good(expected_userid=SENTINELS[0] + "!"),
        good(expected_egress_ip=SENTINELS[1] + "/32"),
        good(secret_names={"app_key": SENTINELS[2] + " x", "secret_key": "ok-1"}),
        good(egress_echo_url="https://" + SENTINELS[0] + ".example/ip"),
        good(per_minute=SENTINELS[1]),
        good(**{SENTINELS[0] + "-key-with-a-long-unsafe-name-padding-more": 1}),
        good(secret_names={"app_key": "a-1", "secret_key": "b-1", SENTINELS[2] + " unsafe name": "x"}),
    ]
    for case in cases:
        with pytest.raises(ConfigError) as err:
            load_relay_config(write(tmp_path, case))
        text = str(err.value) + repr(err.value) + str(err.value.key)
        for sentinel in SENTINELS:
            assert sentinel not in text
    duplicate = '{"' + SENTINELS[0] + '-dup-key-padding-padding-padding-x": 1, "' + SENTINELS[0] + '-dup-key-padding-padding-padding-x": 2}'
    with pytest.raises(ConfigError) as err:
        load_relay_config(write(tmp_path, duplicate))
    assert SENTINELS[0] not in str(err.value)


def test_messages_hold_only_the_code_and_the_key(tmp_path):
    err = refused(tmp_path, good(per_minute=500), "limit_too_high", "per_minute")
    assert str(err) == "limit_too_high:per_minute"
    assert str(refused(tmp_path, GOOD, "file_mode", mode=0o644)) == "file_mode"


def test_repr_hides_the_user_id():
    cfg = RelayConfig(
        expected_egress_ip="203.0.113.7",
        expected_userid="FAKEUSER01",
        secret_names={"app_key": "a-1", "secret_key": "b-1"},
    )
    assert "FAKEUSER01" not in repr(cfg) and "FAKEUSER01" not in str(cfg)


# ----------------------------------------- smoke-driven client settings


class _Transport:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        self.requests.append((method, url, dict(headers), body))
        return HttpResponse(200, {}, self.body)


class _Clock:
    def monotonic(self) -> float:
        return 0.0

    def now_utc(self):
        from datetime import datetime, timezone

        return datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)


def test_the_v2_interval_is_the_smoke_wire_value():
    # 61-SMOKE-BREEZE.json (run 2026-10-21) s2_interval_wire
    assert bc.V2_DAILY_INTERVAL == "1day"
    fake = _Transport(b"{}")
    bc.BreezeClient(fake, clock=_Clock()).daily_bars_v2_raw(
        app_key="K", session_token="T", stock_code="RELIND", exch_code="NSE",
        from_iso="2025-06-09T00:00:00.000Z", to_iso="2025-06-20T23:59:59.000Z",
    )
    assert "interval=1day" in fake.requests[0][1]


def test_status_is_accepted_as_an_int_only():
    # 61-SMOKE-BREEZE.json (run 2026-10-21) status_type is "int" for every call
    good_body = json.dumps({"Success": [1], "Status": 200, "Error": None}).encode()
    string_body = json.dumps({"Success": [1], "Status": "200", "Error": None}).encode()
    assert bc.classify_payload(200, good_body).ok is True
    assert bc.classify_payload(200, string_body).ok is False
    with pytest.raises(bc.BreezeError):
        bc.normalize(200, string_body)


def test_the_bars_request_keeps_the_shipped_shape_when_no_variant_passed():
    # s2_variant_passed and s2_variant_knobs were null: no User-Agent, literal colons,
    # the exch_code parameter name.
    fake = _Transport(b"{}")
    bc.BreezeClient(fake, clock=_Clock()).daily_bars_v2_raw(
        app_key="K", session_token="T", stock_code="RELIND", exch_code="NSE",
        from_iso="2025-06-09T00:00:00.000Z", to_iso="2025-06-20T23:59:59.000Z",
    )
    _method, url, headers, body = fake.requests[0]
    assert url == (
        "https://breezeapi.icicidirect.com/api/v2/historicalcharts?stock_code=RELIND"
        "&exch_code=NSE&from_date=2025-06-09T00:00:00.000Z&to_date=2025-06-20T23:59:59.000Z"
        "&interval=1day&product_type=cash"
    )
    assert "User-Agent" not in headers and body is None
