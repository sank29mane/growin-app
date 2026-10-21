"""The relay as a deployable service (growin-relay/1, Phase 61-08).

Start-up from relay.json and Secret Manager, the internal-IP bind rule, the
persisted day counter, quiet logs, and an event loop that a slow Breeze call
cannot stall. The service runs for real on 127.0.0.1 (real uvicorn, real TCP);
only the upstream network is a FakeTransport. Generated fake values only.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import http.client
import inspect
import io
import json
import logging
import os
import socket
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_gateway_relay_app import (
    APP_KEY,
    BASE,
    BARS_QUERY,
    CODE,
    CONTRACT_HDR,
    EXPECTED_IP,
    SECRET_KEY,
    T0,
    USERID,
    FakeClock,
    FakeTransport,
    Stack,
    auth,
)

from gateway_vm import contract  # noqa: E402
from gateway_vm import main as relay_main  # noqa: E402
from gateway_vm.budget import Budget  # noqa: E402
from gateway_vm.config import load_relay_config  # noqa: E402
from gateway_vm.counter_store import COUNTER_FILE, CounterUnreadable, FileCounterStore  # noqa: E402
from gateway_vm.events import EventLog  # noqa: E402
from gateway_vm.secret_source import (  # noqa: E402
    SecretManagerSource,
    SecretsUnavailable,
    load_credentials,
)
from gateway_vm.session import (  # noqa: E402
    IST,
    BudgetExceeded,
    BudgetUnavailable,
    Credentials,
)
from gateway_vm.transport import HttpResponse, TransportError  # noqa: E402

PKG = Path(__file__).resolve().parents[2] / "gateway" / "vm" / "gateway_vm"

PROJECT_ID = "growin-test-project"
SA_TOKEN = "CANARY-SA-TOKEN-5c1e9a"
APP_SECRET_NAME = "breeze-app-key"
SECRET_SECRET_NAME = "breeze-secret-key"
SECRET_VALUES = {APP_SECRET_NAME: APP_KEY, SECRET_SECRET_NAME: SECRET_KEY}
INTERNAL_IP = "10.128.0.5"
METADATA = "metadata.google.internal"


# ---------------------------------------------------------------- fakes


class RuntimeTransport(FakeTransport):
    """FakeTransport plus the metadata server and Secret Manager."""

    def __init__(self) -> None:
        super().__init__()
        self.internal_ip: str | Exception = INTERNAL_IP
        self.project: HttpResponse | Exception = HttpResponse(200, {}, PROJECT_ID.encode())
        self.token: HttpResponse | Exception = HttpResponse(
            200, {}, json.dumps({"access_token": SA_TOKEN, "expires_in": 3599, "token_type": "Bearer"}).encode()
        )
        self.access: dict[str, HttpResponse | Exception] = {
            name: self.secret_reply(value) for name, value in SECRET_VALUES.items()
        }

    @staticmethod
    def secret_reply(value: str) -> HttpResponse:
        data = base64.b64encode(value.encode()).decode()
        return HttpResponse(200, {}, json.dumps({"name": "x", "payload": {"data": data}}).encode())

    def _record(self, method, url, headers, body) -> None:
        with self.lock:
            self.requests.append((method, url, dict(headers), body))

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == METADATA:
            tail = parts.path.removeprefix("/computeMetadata/v1/")
            if tail == "project/project-id":
                self._record(method, url, headers, body)
                return self._reply(self.project)
            if tail == "instance/service-accounts/default/token":
                self._record(method, url, headers, body)
                return self._reply(self.token)
            if tail == "instance/network-interfaces/0/ip":
                self._record(method, url, headers, body)
                return self._ip(self.internal_ip)
        if parts.hostname == "secretmanager.googleapis.com":
            self._record(method, url, headers, body)
            name = parts.path.split("/secrets/")[1].split("/")[0]
            return self._reply(self.access[name])
        return super().request(method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes)

    def calls_to(self, tail: str) -> list:
        return [r for r in self.requests if urlsplit(r[1]).path.endswith(tail)]


def write_config(tmp_path: Path, **overrides) -> Path:
    body = {
        "expected_egress_ip": EXPECTED_IP,
        "expected_userid": USERID,
        "secret_names": {"app_key": APP_SECRET_NAME, "secret_key": SECRET_SECRET_NAME},
        **overrides,
    }
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(body))
    path.chmod(0o600)
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def live_server(app):
    """The real uvicorn server on 127.0.0.1 in a background thread."""
    port = free_port()
    server = relay_main.build_server(app, host="127.0.0.1", port=port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "server did not start"
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()


def http_get(port: int, path: str, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


class Captured:
    """Everything the process wrote, for the no-leak assertions."""

    def __init__(self, capsys) -> None:
        self._capsys = capsys

    def text(self) -> str:
        out = self._capsys.readouterr()
        return out.out + out.err


def build(tmp_path, transport=None, **kwargs):
    transport = transport or RuntimeTransport()
    config = load_relay_config(write_config(tmp_path))
    sink = io.StringIO()
    clock = kwargs.pop("clock", FakeClock())
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    app = relay_main.build_app(
        config,
        transport=transport,
        secret_source=SecretManagerSource(transport),
        clock=clock,
        events=EventLog(sink, clock),
        store=kwargs.pop("store", FileCounterStore(state)),
    )
    return app, transport, sink, clock


# ------------------------------------------------------------- tracer


def test_tracer_service_starts_from_relay_json_and_secret_manager_over_real_tcp(tmp_path, capsys):
    app, transport, sink, _clock = build(tmp_path)
    with live_server(app) as port:
        status, headers, body = http_get(port, "/v1/health", dict(CONTRACT_HDR))
        assert status == 200
        assert headers[contract.HDR_CONTRACT.lower()] == contract.CONTRACT
        assert json.loads(body)["state"] == "none"
        assert json.loads(body)["contract"] == contract.CONTRACT

        status, headers, body = http_get(port, "/v1/health")  # no contract header
        assert status == 403
        assert json.loads(body)["error"]["code"] == "FORBIDDEN_CLIENT"
        assert headers[contract.HDR_CONTRACT.lower()] == contract.CONTRACT
        assert "server" not in headers  # server_header is off

    assert len(transport.calls_to("/project/project-id")) == 1
    assert len(transport.calls_to("/service-accounts/default/token")) == 1
    access = transport.calls_to(":access")
    assert sorted(urlsplit(r[1]).path.split("/secrets/")[1].split("/")[0] for r in access) == sorted(SECRET_VALUES)
    for _method, url, headers, _body in access:
        assert headers == {"Authorization": f"Bearer {SA_TOKEN}"}
        assert url.startswith(f"https://secretmanager.googleapis.com/v1/projects/{PROJECT_ID}/secrets/")
        assert url.endswith("/versions/latest:access")
    for call in transport.calls_to("/project/project-id") + transport.calls_to("/service-accounts/default/token"):
        assert call[2] == {"Metadata-Flavor": "Google"}

    seen = capsys.readouterr()
    for text in (seen.out, seen.err, sink.getvalue()):
        for secret in (SA_TOKEN, APP_KEY, SECRET_KEY):
            assert secret not in text
        assert "Traceback" not in text


# ------------------------------------------------------------ secrets


def source_for(transport) -> SecretManagerSource:
    return SecretManagerSource(transport)


def test_credentials_repr_shows_no_value():
    creds = load_credentials(source_for(RuntimeTransport()), {"app_key": APP_SECRET_NAME, "secret_key": SECRET_SECRET_NAME})
    assert (creds.app_key, creds.secret_key) == (APP_KEY, SECRET_KEY)
    assert APP_KEY not in repr(creds) and SECRET_KEY not in repr(creds)
    assert APP_KEY not in str(creds) and SECRET_KEY not in str(creds)


@pytest.mark.parametrize(
    "value",
    ["has space", "tab\there", "line\nbreak", "trailing\n", "ctrl\x07", "", "x" * 600, "café"],
)
def test_secret_payload_with_whitespace_or_control_characters_is_malformed(value):
    transport = RuntimeTransport()
    transport.access[APP_SECRET_NAME] = transport.secret_reply(value) if value.isascii() else HttpResponse(
        200, {}, json.dumps({"payload": {"data": base64.b64encode(value.encode()).decode()}}).encode()
    )
    with pytest.raises(SecretsUnavailable) as err:
        source_for(transport).read(APP_SECRET_NAME)
    assert err.value.code == "malformed"
    assert str(err.value) == "malformed"


def _meta_fail(t):
    t.project = TransportError("connect", METADATA)


def _meta_500(t):
    t.project = HttpResponse(500, {}, b"")


def _meta_bad_project(t):
    t.project = HttpResponse(200, {}, b"Not A Project!")


def _token_not_json(t):
    t.token = HttpResponse(200, {}, b"<html>")


def _token_missing(t):
    t.token = HttpResponse(200, {}, b'{"expires_in": 5}')


def _token_not_string(t):
    t.token = HttpResponse(200, {}, b'{"access_token": 123}')


def _token_with_space(t):
    t.token = HttpResponse(200, {}, b'{"access_token": "a b"}')


def _status(code):
    def apply(t):
        t.access[APP_SECRET_NAME] = HttpResponse(code, {}, b'{"error": {"message": "CANARY-ERR-BODY"}}')

    return apply


def _bad_json(t):
    t.access[APP_SECRET_NAME] = HttpResponse(200, {}, b"not json")


def _no_payload(t):
    t.access[APP_SECRET_NAME] = HttpResponse(200, {}, b'{"name": "x"}')


def _bad_base64(t):
    t.access[APP_SECRET_NAME] = HttpResponse(200, {}, b'{"payload": {"data": "@@@@"}}')


def _sm_down(t):
    t.access[APP_SECRET_NAME] = TransportError("timeout", "secretmanager.googleapis.com")


@pytest.mark.parametrize(
    "mutate, code",
    [
        (_meta_fail, "metadata_unreachable"),
        (_meta_500, "metadata_unreachable"),
        (_meta_bad_project, "malformed"),
        (_token_not_json, "token_invalid"),
        (_token_missing, "token_invalid"),
        (_token_not_string, "token_invalid"),
        (_token_with_space, "token_invalid"),
        (_status(401), "token_invalid"),
        (_status(403), "access_denied"),
        (_status(404), "not_found"),
        (_status(500), "malformed"),
        (_bad_json, "malformed"),
        (_no_payload, "malformed"),
        (_bad_base64, "malformed"),
        (_sm_down, "malformed"),
    ],
)
def test_secret_failures_map_to_a_closed_code_with_no_detail(mutate, code):
    transport = RuntimeTransport()
    mutate(transport)
    with pytest.raises(SecretsUnavailable) as err:
        source_for(transport).read(APP_SECRET_NAME)
    assert err.value.code == code
    text = f"{err.value!s} {err.value!r}"
    assert text.count(code) >= 1
    for leaked in (SA_TOKEN, PROJECT_ID, "CANARY-ERR-BODY", APP_SECRET_NAME):
        assert leaked not in text


@pytest.mark.parametrize("name", ["", "a b", "../x", "x/y", "x" * 256, "name\n", "café"])
def test_a_bad_secret_name_is_malformed_and_sends_nothing(name):
    transport = RuntimeTransport()
    with pytest.raises(SecretsUnavailable) as err:
        source_for(transport).read(name)
    assert err.value.code == "malformed"
    assert transport.requests == []


def test_load_credentials_drops_the_access_token_after_success_and_after_failure():
    source = source_for(RuntimeTransport())
    load_credentials(source, {"app_key": APP_SECRET_NAME, "secret_key": SECRET_SECRET_NAME})
    assert source._token is None and source._project is None

    transport = RuntimeTransport()
    transport.access[SECRET_SECRET_NAME] = HttpResponse(403, {}, b"{}")
    source = source_for(transport)
    with pytest.raises(SecretsUnavailable):
        load_credentials(source, {"app_key": APP_SECRET_NAME, "secret_key": SECRET_SECRET_NAME})
    assert source._token is None and source._project is None


def test_load_credentials_needs_both_names():
    with pytest.raises(SecretsUnavailable) as err:
        load_credentials(source_for(RuntimeTransport()), {"app_key": APP_SECRET_NAME})
    assert err.value.code == "malformed"


# ------------------------------------------------------- bind address


@pytest.mark.parametrize("ip", ["10.0.0.1", INTERNAL_IP, "10.255.255.254", "172.16.0.4", "172.31.255.1", "192.168.1.9"])
def test_resolve_bind_address_returns_a_private_address(ip):
    transport = RuntimeTransport()
    transport.internal_ip = ip
    assert relay_main.resolve_bind_address(transport) == ip
    call = transport.calls_to("/network-interfaces/0/ip")[0]
    assert call[2] == {"Metadata-Flavor": "Google"}


@pytest.mark.parametrize(
    "answer",
    [
        "0.0.0.0", "127.0.0.1", "169.254.169.254", "8.8.8.8", "::1", "fe80::1", "224.0.0.1", "255.255.255.255",
        "172.15.0.1", "172.32.0.1", "192.169.0.1", "100.64.0.1", "11.0.0.1",
        "internal.example", "localhost", "", "  ", "10.0.0", "10.0.0.256", "010.0.0.1", "10.0.0.1/8", "10.0.0.1\n10.0.0.2",
        "0x0a000001", "167772161",
    ],
)
def test_resolve_bind_address_refuses_everything_else(answer):
    transport = RuntimeTransport()
    transport.internal_ip = answer
    with pytest.raises(ValueError) as err:
        relay_main.resolve_bind_address(transport)
    assert answer.strip() == "" or answer.strip() not in str(err.value)


def test_resolve_bind_address_refuses_an_unreachable_or_failing_metadata_server():
    for reply in (TransportError("connect", METADATA), TransportError("timeout", METADATA)):
        transport = RuntimeTransport()
        transport.internal_ip = reply  # type: ignore[assignment]
        with pytest.raises(ValueError):
            relay_main.resolve_bind_address(transport)
    transport = RuntimeTransport()

    def failing(*_a, **_k):
        return HttpResponse(500, {}, INTERNAL_IP.encode())

    transport.request = failing  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        relay_main.resolve_bind_address(transport)


# --------------------------------------------------------------- main


class Runner:
    def __init__(self) -> None:
        self.servers: list = []

    def __call__(self, server) -> None:
        self.servers.append(server)


def run_main(tmp_path, argv=None, *, transport=None, secret_source_factory=SecretManagerSource, environ=None, config=True):
    transport = transport or RuntimeTransport()
    runner = Runner()
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    config_path = write_config(tmp_path) if config is True else config
    closed = []
    original_close = getattr(transport, "close", None)

    def close():
        closed.append(True)
        if original_close:
            original_close()

    transport.close = close  # type: ignore[attr-defined]
    code = relay_main.main(
        argv if argv is not None else ["--host", "internal-ip", "--port", "8443", "--config", str(config_path)],
        transport_factory=lambda: transport,
        secret_source_factory=secret_source_factory,
        environ=environ if environ is not None else {"STATE_DIRECTORY": str(state)},
        run=runner,
    )
    return code, runner, transport, closed


def stdout_events(capsys) -> list[dict]:
    out = capsys.readouterr()
    assert "Traceback" not in out.out + out.err
    return [json.loads(line) for line in out.out.splitlines()]


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", INTERNAL_IP, "localhost", "::", "", "INTERNAL-IP", "internal-ip "])
def test_main_refuses_any_literal_host_before_any_network(tmp_path, capsys, host):
    code, runner, transport, _closed = run_main(
        tmp_path, ["--host", host, "--port", "8443", "--config", str(write_config(tmp_path))]
    )
    assert code == 2
    assert runner.servers == []
    assert transport.requests == []  # not even the metadata server was asked
    events = stdout_events(capsys)
    assert [(e["event"], e["result"], e["reason"]) for e in events] == [("service_start", "error", "bind_invalid")]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--port", "8443"],
        ["--host", "internal-ip", "--port", "80"],
        ["--host", "internal-ip", "--port", "70000"],
        ["--host", "internal-ip", "--port", "abc"],
        ["--host", "internal-ip", "--bogus"],
    ],
)
def test_main_refuses_bad_arguments_with_exit_2_and_one_event(tmp_path, capsys, argv):
    code, runner, transport, _ = run_main(tmp_path, argv)
    assert code == 2 and runner.servers == [] and transport.requests == []
    assert len(stdout_events(capsys)) == 1


def test_main_internal_ip_binds_the_metadata_address_and_runs(tmp_path, capsys):
    code, runner, transport, closed = run_main(tmp_path)
    assert code == 0
    assert len(runner.servers) == 1
    assert runner.servers[0].config.host == INTERNAL_IP
    assert runner.servers[0].config.port == 8443
    assert closed == [True]  # the pool is closed when the server stops
    events = stdout_events(capsys)
    assert [(e["event"], e["result"]) for e in events] == [("service_start", "ok")]
    assert "reason" not in events[0]
    assert len(transport.calls_to("/network-interfaces/0/ip")) == 1


def test_main_refuses_a_public_metadata_address_with_one_event(tmp_path, capsys):
    transport = RuntimeTransport()
    transport.internal_ip = "8.8.8.8"
    code, runner, _t, closed = run_main(tmp_path, transport=transport)
    assert code == 2 and runner.servers == [] and closed == [True]
    events = stdout_events(capsys)
    assert [(e["event"], e["result"], e["reason"]) for e in events] == [("service_start", "error", "bind_invalid")]
    assert transport.calls_to(":access") == []  # secrets are not read for a refused bind


def test_main_with_an_unfilled_config_exits_3_with_one_event(tmp_path, capsys):
    config = write_config(tmp_path, expected_userid="<EXPECTED_USERID>")
    code, runner, transport, _ = run_main(tmp_path, config=config)
    assert code == 3 and runner.servers == [] and transport.requests == []
    events = stdout_events(capsys)
    assert [(e["event"], e["result"], e["reason"]) for e in events] == [("service_start", "error", "config_invalid")]


def test_main_without_a_config_path_or_credentials_directory_exits_3(tmp_path, capsys):
    code, runner, _t, _c = run_main(tmp_path, ["--host", "internal-ip"], environ={"STATE_DIRECTORY": str(tmp_path)})
    assert code == 3 and runner.servers == []
    assert [e["reason"] for e in stdout_events(capsys)] == ["config_invalid"]


def test_main_defaults_the_config_to_credentials_directory_relay_json(tmp_path, capsys):
    write_config(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    code, runner, _t, _c = run_main(
        tmp_path,
        ["--host", "internal-ip"],
        environ={"CREDENTIALS_DIRECTORY": str(tmp_path), "STATE_DIRECTORY": str(state)},
    )
    assert code == 0 and len(runner.servers) == 1
    assert runner.servers[0].config.port == 8443  # the default


class RaisingSource:
    def __init__(self, transport) -> None:
        self.transport = transport

    def read(self, name):
        raise SecretsUnavailable("access_denied")


def test_main_exits_4_with_one_event_when_secrets_cannot_be_read(tmp_path, capsys):
    code, runner, _t, closed = run_main(tmp_path, secret_source_factory=RaisingSource)
    assert code == 4 and runner.servers == [] and closed == [True]
    events = stdout_events(capsys)
    assert [(e["event"], e["result"], e["reason"]) for e in events] == [("service_start", "error", "secrets_unavailable")]


def test_main_exit_4_on_a_real_secret_manager_denial_leaks_nothing(tmp_path, capsys):
    transport = RuntimeTransport()
    transport.access[SECRET_SECRET_NAME] = HttpResponse(403, {}, b'{"error": "CANARY-ERR-BODY"}')
    code, _runner, _t, _c = run_main(tmp_path, transport=transport)
    assert code == 4
    text = json.dumps(stdout_events(capsys))
    for leaked in (SA_TOKEN, APP_KEY, PROJECT_ID, "CANARY-ERR-BODY"):
        assert leaked not in text


def test_main_turns_an_unexpected_failure_into_exit_1_and_no_traceback(tmp_path, capsys, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("CANARY-INTERNAL-DETAIL")

    monkeypatch.setattr(relay_main, "build_app", boom)
    code, runner, _t, closed = run_main(tmp_path)
    assert code == 1 and runner.servers == [] and closed == [True]
    out = capsys.readouterr()
    assert "CANARY-INTERNAL-DETAIL" not in out.out + out.err and "Traceback" not in out.out + out.err
    assert [json.loads(line)["reason"] for line in out.out.splitlines()] == ["internal"]


def test_main_reads_exactly_two_environment_variables(tmp_path):
    class Spy(dict):
        def __init__(self, *a):
            super().__init__(*a)
            self.reads: list[str] = []

        def get(self, key, default=None):
            self.reads.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.reads.append(key)
            return super().__getitem__(key)

    state = tmp_path / "state"
    state.mkdir()
    environ = Spy({"CREDENTIALS_DIRECTORY": str(tmp_path), "STATE_DIRECTORY": str(state), "BREEZE_SECRET": "x"})
    write_config(tmp_path)
    code, *_ = run_main(tmp_path, ["--host", "internal-ip"], environ=environ)
    assert code == 0
    assert set(environ.reads) == {"CREDENTIALS_DIRECTORY", "STATE_DIRECTORY"}


def test_production_wiring_uses_one_pooled_transport_for_every_upstream_caller(tmp_path, monkeypatch):
    from gateway_vm.pool import PooledTransport

    defaults = inspect.signature(relay_main.main).parameters
    assert defaults["transport_factory"].default is PooledTransport
    assert defaults["secret_source_factory"].default is SecretManagerSource

    seen: dict[str, object] = {}
    real_build_app = relay_main.build_app

    def spy_build_app(config, *, transport, secret_source, **kwargs):
        seen["build_app"] = transport
        seen["secret_source"] = secret_source._transport
        return real_build_app(config, transport=transport, secret_source=secret_source, **kwargs)

    monkeypatch.setattr(relay_main, "build_app", spy_build_app)
    code, _runner, transport, _c = run_main(tmp_path)
    assert code == 0
    assert seen["build_app"] is transport and seen["secret_source"] is transport

    source = inspect.getsource(relay_main.build_app)
    assert source.count("transport") >= 4  # egress, breeze and the secret source all take the one argument
    tree = ast.parse(inspect.getsource(relay_main))
    constructed = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("PooledTransport", "HttpClientTransport")
    ]
    assert constructed == []  # built only through the one injectable factory default


def test_state_directory_unset_or_not_a_directory_starts_with_one_alarm_and_refuses_breeze(tmp_path, capsys):
    for environ in ({}, {"STATE_DIRECTORY": str(tmp_path / "missing")}, {"STATE_DIRECTORY": str(write_config(tmp_path))}):
        code, runner, transport, _c = run_main(tmp_path, environ=environ)
        assert code == 0 and len(runner.servers) == 1
        events = stdout_events(capsys)
        assert [(e["event"], e.get("reason")) for e in events] == [
            ("config_alarm", "counter_unreadable"),
            ("service_start", None),
        ]
        client = TestClient(runner.servers[0].config.app, base_url=BASE)
        health = client.get("/v1/health", headers=CONTRACT_HDR)
        assert health.status_code == 200
        armed = client.post("/v1/login/arm", json={}, headers=CONTRACT_HDR).json()
        done = client.post(
            "/v1/login/complete",
            json={"attempt_id": armed["attempt_id"], "apisession": CODE},
            headers={**CONTRACT_HDR, **auth(armed["relay_token"])},
        )
        assert done.status_code == 503 and done.json()["error"]["code"] == "BUDGET_UNAVAILABLE"
        assert done.json()["error"]["reason"] == "counter_unreadable"
        assert not [r for r in transport.requests if r[1].endswith("/customerdetails")]
        capsys.readouterr()  # the request events above belong to this pass, not the next


# -------------------------------------------------------- uvicorn setup


def test_build_server_is_quiet_and_does_not_trust_proxies(tmp_path):
    app, *_ = build(tmp_path)
    server = relay_main.build_server(app, host="127.0.0.1", port=free_port())
    config = server.config
    assert config.access_log is False
    assert config.log_level == "warning"
    assert config.server_header is False
    assert config.proxy_headers is False
    assert config.log_config == relay_main.LOG_CONFIG
    assert (config.http, config.loop, config.lifespan) == ("h11", "asyncio", "off")
    assert config.host == "127.0.0.1"


def test_log_config_routes_uvicorn_to_stderr_at_warning_and_silences_the_access_log():
    cfg = relay_main.LOG_CONFIG
    handlers = cfg["handlers"]
    assert handlers["stderr"]["stream"] == "ext://sys.stderr"
    assert handlers["stderr"]["level"] == "WARNING"
    for name in ("uvicorn", "uvicorn.error"):
        assert cfg["loggers"][name]["handlers"] == ["stderr"] and cfg["loggers"][name]["level"] == "WARNING"
        assert cfg["loggers"][name]["propagate"] is False
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["null"]
    assert handlers["null"]["class"] == "logging.NullHandler"
    assert cfg["loggers"]["uvicorn.access"]["propagate"] is False
    assert cfg["formatters"]["plain"]["()"] is relay_main.NoTracebackFormatter


def test_no_traceback_formatter_prints_a_fixed_string_for_exceptions_and_stacks():
    fmt = relay_main.NoTracebackFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError("CANARY-EXC-TEXT")
    except RuntimeError:
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application", (), sys.exc_info())
    record.stack_info = "Stack (most recent call last): CANARY-STACK-TEXT"
    line = fmt.format(record)
    assert "Traceback" not in line and "CANARY" not in line and "RuntimeError" not in line
    assert "Exception in ASGI application" in line
    assert fmt.formatException((None, None, None)) == "[traceback suppressed]"  # type: ignore[arg-type]
    assert fmt.formatStack("anything") == "[stack suppressed]"


def test_the_live_server_writes_no_request_line_query_or_token_to_stdout_or_stderr(tmp_path, capsys):
    app, transport, sink, _clock = build(tmp_path)
    capsys.readouterr()
    with live_server(app) as port:
        bearer = {**CONTRACT_HDR, "Authorization": "Bearer " + "A" * 43}
        http_get(port, "/v1/bars/daily?stock_code=CANARYQUERYVALUE&from=2025-06-02&to=2025-06-13", bearer)
        http_get(port, "/v1/health", dict(CONTRACT_HDR))
        http_get(port, "/no/such/path?secret=CANARYQUERYVALUE", dict(CONTRACT_HDR))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.sock = None
        conn.connect()
        conn.sock.sendall(b"NOT HTTP AT ALL CANARYQUERYVALUE\r\n\r\n")  # malformed request line
        with contextlib.suppress(Exception):
            conn.sock.recv(1024)
        conn.close()
        time.sleep(0.2)
    seen = capsys.readouterr()
    for text in (seen.out, seen.err, sink.getvalue()):
        assert "CANARYQUERYVALUE" not in text
        assert "GET /" not in text and "HTTP/1.1" not in text
        assert "Traceback" not in text
        assert "A" * 43 not in text


# ----------------------------------------------------------- budget


class Tick:
    """A clock the budget tests can move by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.mono = 5000.0
        self.now = now

    def monotonic(self) -> float:
        return self.mono

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


class MemoryStore:
    def __init__(self, value=None) -> None:
        self.value = value
        self.saves: list[tuple[date, int]] = []
        self.fail_load = False
        self.fail_save = False

    def load(self):
        if self.fail_load:
            raise CounterUnreadable
        return self.value

    def save(self, ist_date, count):
        if self.fail_save:
            raise CounterUnreadable
        self.value = (ist_date, count)
        self.saves.append((ist_date, count))


def make_budget(clock=None, store=None, **kwargs) -> tuple[Budget, Tick, object]:
    clock = clock or Tick()
    store = store if store is not None else MemoryStore()
    params = {"per_minute": 80, "per_day": 4500, "security_master_per_hour": 6, **kwargs}
    return Budget(clock, store=store, **params), clock, store


def test_rolling_minute_window_allows_80_then_refuses_the_81st_until_the_window_passes():
    budget, clock, _store = make_budget()
    for _ in range(80):
        budget.charge("breeze")
        clock.advance(0.5)  # 80 calls inside 40 s
    with pytest.raises(BudgetExceeded) as err:
        budget.charge("breeze")
    assert err.value.reason == "budget_minute"
    assert 1 <= err.value.retry_after_s <= 60
    clock.advance(60)
    budget.charge("breeze")  # the whole window has rolled off


def test_rolling_minute_retry_after_is_the_time_until_the_oldest_call_leaves():
    budget, clock, _ = make_budget(per_minute=2)
    budget.charge("breeze")
    clock.advance(20)
    budget.charge("breeze")
    with pytest.raises(BudgetExceeded) as err:
        budget.charge("breeze")
    assert err.value.retry_after_s == 40
    clock.advance(40)
    budget.charge("breeze")  # exactly 60 s after the first


def test_security_master_allows_6_per_rolling_hour_then_refuses():
    budget, clock, store = make_budget()
    for _ in range(6):
        budget.charge("security_master")
        clock.advance(60)
    with pytest.raises(BudgetExceeded) as err:
        budget.charge("security_master")
    assert err.value.reason == "security_master_hourly"
    assert 1 <= err.value.retry_after_s <= 3600
    clock.advance(3600)
    budget.charge("security_master")
    assert store.saves == []  # never a Breeze call, never the counter file


def test_security_master_does_not_use_the_breeze_windows():
    budget, _clock, _store = make_budget(per_minute=1)
    budget.charge("breeze")
    budget.charge("security_master")
    with pytest.raises(BudgetExceeded):
        budget.charge("breeze")


def test_an_unknown_kind_is_refused():
    budget, *_ = make_budget()
    with pytest.raises(ValueError):
        budget.charge("orders")


def test_snapshot_reports_the_counters_and_the_ist_date():
    budget, clock, _ = make_budget()
    assert budget.snapshot() == {
        "calls_last_minute": 0,
        "calls_today_ist": 0,
        "per_minute_limit": 80,
        "per_day_limit": 4500,
        "remaining_today": 4500,
        "ist_date": "2026-10-21",
    }
    for _ in range(3):
        budget.charge("breeze")
    budget.charge("security_master")
    snap = budget.snapshot()
    assert (snap["calls_last_minute"], snap["calls_today_ist"], snap["remaining_today"]) == (3, 3, 4497)
    clock.advance(61)
    assert budget.snapshot()["calls_last_minute"] == 0
    assert budget.snapshot()["calls_today_ist"] == 3
    for value in budget.snapshot().values():
        assert isinstance(value, (int, str)) and not isinstance(value, bool)


def test_snapshot_never_raises_and_says_nothing_remains_when_the_counter_is_unreadable():
    store = MemoryStore()
    store.fail_load = True
    budget, *_ = make_budget(store=store)
    snap = budget.snapshot()
    assert snap["remaining_today"] == 0 and snap["calls_today_ist"] == 4500
    assert snap["ist_date"] == "2026-10-21"


def test_each_breeze_charge_is_persisted_before_it_returns(tmp_path):
    store = FileCounterStore(tmp_path)
    budget, *_ = make_budget(store=store)
    for expected in (1, 2, 3):
        budget.charge("breeze")
        path = tmp_path / COUNTER_FILE
        assert json.loads(path.read_text()) == {"ist_date": "2026-10-21", "count": expected}
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [COUNTER_FILE]  # no temp file left behind


def test_the_save_happens_before_the_charge_returns_and_a_failed_save_counts_nothing():
    store = MemoryStore()
    budget, *_ = make_budget(store=store)
    budget.charge("breeze")
    assert store.saves == [(date(2026, 10, 21), 1)]
    store.fail_save = True
    with pytest.raises(BudgetUnavailable) as err:
        budget.charge("breeze")
    assert err.value.reason == "counter_unreadable"
    assert budget.snapshot()["remaining_today"] == 0  # the numbers say what the gate does: nothing is allowed
    store.fail_save = False
    budget.charge("breeze")  # a later good save recovers
    assert budget.snapshot()["calls_today_ist"] == 2
    assert budget.snapshot()["calls_last_minute"] == 2
    assert store.saves[-1] == (date(2026, 10, 21), 2)


def test_a_restart_continues_the_persisted_count_to_the_day_ceiling(tmp_path):
    store = FileCounterStore(tmp_path)
    store.save(date(2026, 10, 21), 4499)
    clock = Tick()
    first, *_ = make_budget(clock=clock, store=FileCounterStore(tmp_path))  # a "restart": new object, same file
    first.charge("breeze")  # the 4,500th
    assert json.loads((tmp_path / COUNTER_FILE).read_text())["count"] == 4500
    with pytest.raises(BudgetExceeded) as err:
        first.charge("breeze")
    assert err.value.reason == "budget_day"
    ist_midnight = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
    assert err.value.retry_after_s == int((ist_midnight - clock.now).total_seconds())

    second, *_ = make_budget(clock=clock, store=FileCounterStore(tmp_path))  # restart again
    with pytest.raises(BudgetExceeded) as err:
        second.charge("breeze")
    assert err.value.reason == "budget_day"

    clock.now = ist_midnight - timedelta(seconds=1)
    with pytest.raises(BudgetExceeded):
        second.charge("breeze")
    clock.advance(1)  # 18:30 UTC is IST midnight
    second.charge("breeze")
    assert json.loads((tmp_path / COUNTER_FILE).read_text()) == {"ist_date": "2026-10-22", "count": 1}


def test_a_missing_file_starts_at_zero_for_today_and_the_minute_reset_does_not_reset_the_day(tmp_path):
    store = FileCounterStore(tmp_path)
    budget, clock, _ = make_budget(store=store)
    assert not (tmp_path / COUNTER_FILE).exists()
    budget.charge("breeze")
    clock.advance(61)  # the minute window empties; the day count stays
    budget.charge("breeze")
    assert json.loads((tmp_path / COUNTER_FILE).read_text()) == {"ist_date": "2026-10-21", "count": 2}
    assert budget.snapshot()["calls_last_minute"] == 1


def test_a_count_for_an_older_ist_date_restarts_at_one(tmp_path):
    store = FileCounterStore(tmp_path)
    store.save(date(2026, 10, 20), 4000)
    budget, *_ = make_budget(store=store)
    assert budget.snapshot()["calls_today_ist"] == 0
    budget.charge("breeze")
    assert json.loads((tmp_path / COUNTER_FILE).read_text()) == {"ist_date": "2026-10-21", "count": 1}


def test_the_ist_date_not_the_utc_date_rolls_the_day(tmp_path):
    clock = Tick(datetime(2026, 10, 21, 18, 29, 59, tzinfo=timezone.utc))  # 23:59:59 IST
    store = FileCounterStore(tmp_path)
    budget, *_ = make_budget(clock=clock, store=store, per_day=1)
    budget.charge("breeze")
    with pytest.raises(BudgetExceeded):
        budget.charge("breeze")
    clock.advance(1)  # 00:00:00 IST on the 22nd, still the 21st in UTC
    assert clock.now_utc().date() == date(2026, 10, 21)
    budget.charge("breeze")
    assert json.loads((tmp_path / COUNTER_FILE).read_text())["ist_date"] == "2026-10-22"


def _write(content: str):
    def apply(directory: Path) -> Path:
        (directory / COUNTER_FILE).write_text(content)
        return directory

    return apply


def _symlink(directory: Path) -> Path:
    target = directory / "elsewhere"
    target.write_text('{"ist_date": "2026-10-21", "count": 0}')
    (directory / COUNTER_FILE).symlink_to(target)
    return directory


def _as_directory(directory: Path) -> Path:
    (directory / COUNTER_FILE).mkdir()
    return directory


def _read_only(directory: Path) -> Path:
    directory.chmod(0o500)
    return directory


def _missing_directory(directory: Path) -> Path:
    return directory / "gone"


def _a_file_not_a_directory(directory: Path) -> Path:
    path = directory / "plain-file"
    path.write_text("x")
    return path


COUNTER_FAULTS = {
    "garbage": _write("not json at all"),
    "empty": _write(""),
    "not an object": _write("[1, 2]"),
    "missing count": _write('{"ist_date": "2026-10-21"}'),
    "missing date": _write('{"count": 1}'),
    "extra key": _write('{"ist_date": "2026-10-21", "count": 1, "note": "x"}'),
    "negative count": _write('{"ist_date": "2026-10-21", "count": -1}'),
    "float count": _write('{"ist_date": "2026-10-21", "count": 1.0}'),
    "string count": _write('{"ist_date": "2026-10-21", "count": "1"}'),
    "bool count": _write('{"ist_date": "2026-10-21", "count": true}'),
    "huge count": _write('{"ist_date": "2026-10-21", "count": 1e999}'),
    "duplicate key": _write('{"ist_date": "2026-10-21", "count": 1, "count": 2}'),
    "NaN": _write('{"ist_date": "2026-10-21", "count": NaN}'),
    "malformed date": _write('{"ist_date": "21-10-2026", "count": 1}'),
    "impossible date": _write('{"ist_date": "2026-02-30", "count": 1}'),
    "date with newline": _write('{"ist_date": "2026-10-21\\n", "count": 1}'),
    "non-string date": _write('{"ist_date": 20261021, "count": 1}'),
    "future date": _write('{"ist_date": "2026-10-22", "count": 1}'),
    "oversize": _write('{"ist_date": "2026-10-21", "count": 1, "pad": "' + "x" * 5000 + '"}'),
    "oversize but valid": _write('{"ist_date": "2026-10-21", "count": 1}' + " " * 5000),
    "iso basic date": _write('{"ist_date": "20261021", "count": 1}'),
    "iso week date": _write('{"ist_date": "2026-W43-3", "count": 1}'),
    "symlink": _symlink,
    "directory": _as_directory,
    "read-only dir": _read_only,
    "missing dir": _missing_directory,
    "not a dir": _a_file_not_a_directory,
}


@pytest.mark.parametrize("fault", list(COUNTER_FAULTS))
def test_a_counter_fault_refuses_every_breeze_charge_and_never_guesses(tmp_path, fault):
    state = tmp_path / "state"
    state.mkdir()
    state = COUNTER_FAULTS[fault](state)
    try:
        budget, *_ = make_budget(store=FileCounterStore(state))
        for _ in range(2):
            with pytest.raises(BudgetUnavailable) as err:
                budget.charge("breeze")
            assert err.value.reason == "counter_unreadable"
        assert budget.snapshot()["remaining_today"] == 0
        budget.charge("security_master")  # not a Breeze call: unaffected
    finally:
        if state.exists() and state.is_dir():
            state.chmod(0o700)


def test_a_counter_fault_through_the_app_sends_no_breeze_request(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / COUNTER_FILE).write_text("garbage")
    budget, *_ = make_budget(store=FileCounterStore(state))
    stack = Stack(budget=budget)
    token = None
    armed = stack.arm().json()
    done = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert done.status_code == 503 and done.json()["error"]["code"] == "BUDGET_UNAVAILABLE"
    assert stack.transport.breeze_requests() == []  # not even customerdetails
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).status_code == 200
    assert stack.client.get("/v1/budget", headers=CONTRACT_HDR).status_code == 200
    assert token is None


def test_a_counter_that_becomes_unwritable_later_refuses_the_next_call(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    budget, *_ = make_budget(store=FileCounterStore(state))
    budget.charge("breeze")
    state.chmod(0o500)
    try:
        with pytest.raises(BudgetUnavailable):
            budget.charge("breeze")
    finally:
        state.chmod(0o700)
    budget.charge("breeze")
    assert json.loads((state / COUNTER_FILE).read_text())["count"] == 2


def test_a_clock_that_moves_back_before_the_cached_date_refuses(tmp_path):
    clock = Tick()
    budget, *_ = make_budget(clock=clock, store=FileCounterStore(tmp_path))
    budget.charge("breeze")
    clock.now -= timedelta(days=2)
    with pytest.raises(BudgetUnavailable):
        budget.charge("breeze")


def test_eight_threads_charging_a_hundred_times_leave_the_file_equal_to_the_successes(tmp_path):
    budget, *_ = make_budget(store=FileCounterStore(tmp_path), per_minute=10_000, per_day=10_000)
    ok = []
    lock = threading.Lock()

    def worker():
        for _ in range(100):
            budget.charge("breeze")
            with lock:
                ok.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 800
    assert json.loads((tmp_path / COUNTER_FILE).read_text())["count"] == 800


def test_eight_threads_never_exceed_the_ceiling_and_the_file_matches(tmp_path):
    budget, *_ = make_budget(store=FileCounterStore(tmp_path), per_minute=10_000, per_day=500)
    ok, refused = [], []
    lock = threading.Lock()

    def worker():
        for _ in range(100):
            try:
                budget.charge("breeze")
                with lock:
                    ok.append(1)
            except BudgetExceeded:
                with lock:
                    refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(ok), len(refused)) == (500, 300)
    assert json.loads((tmp_path / COUNTER_FILE).read_text())["count"] == 500


# ------------------------------------------------------ counter store


def test_counter_store_none_directory_is_unavailable_for_load_and_save():
    store = FileCounterStore(None)
    with pytest.raises(CounterUnreadable):
        store.load()
    with pytest.raises(CounterUnreadable):
        store.save(date(2026, 10, 21), 1)


def test_counter_store_load_is_none_only_when_no_file_exists(tmp_path):
    store = FileCounterStore(tmp_path)
    assert store.load() is None
    store.save(date(2026, 10, 21), 7)
    assert store.load() == (date(2026, 10, 21), 7)


def test_counter_store_file_has_exactly_two_keys_and_mode_0600_whatever_the_umask(tmp_path):
    old = os.umask(0)
    try:
        FileCounterStore(tmp_path).save(date(2026, 10, 21), 3)
    finally:
        os.umask(old)
    path = tmp_path / COUNTER_FILE
    assert set(json.loads(path.read_text())) == {"ist_date", "count"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_counter_store_accepts_a_symlinked_state_directory_but_not_a_symlinked_file(tmp_path):
    real = tmp_path / "private"
    real.mkdir()
    link = tmp_path / "state"
    link.symlink_to(real)
    store = FileCounterStore(link)  # systemd DynamicUser state directories are symlinks
    store.save(date(2026, 10, 21), 1)
    assert store.load() == (date(2026, 10, 21), 1)
    (real / COUNTER_FILE).unlink()
    (real / "other").write_text('{"ist_date": "2026-10-21", "count": 1}')
    (real / COUNTER_FILE).symlink_to(real / "other")
    with pytest.raises(CounterUnreadable):
        store.load()


def test_counter_store_save_replaces_a_symlink_instead_of_writing_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("original")
    (tmp_path / COUNTER_FILE).symlink_to(victim)
    FileCounterStore(tmp_path).save(date(2026, 10, 21), 1)
    assert victim.read_text() == "original"
    assert not (tmp_path / COUNTER_FILE).is_symlink()


def test_counter_store_exceptions_carry_no_detail(tmp_path):
    (tmp_path / COUNTER_FILE).write_text("CANARY-GARBAGE")
    with pytest.raises(CounterUnreadable) as err:
        FileCounterStore(tmp_path).load()
    assert "CANARY" not in repr(err.value) and str(err.value) == ""


# ------------------------------------------------------- event loop


class SlowBars(FakeTransport):
    """A Breeze bars call that takes one second, like a slow upstream."""

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        if urlsplit(url).hostname == "breezeapi.icicidirect.com":
            time.sleep(1.0)
        return super().request(method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes)


def test_a_slow_breeze_call_does_not_stall_the_event_loop():
    stack = Stack()
    stack.transport = SlowBars()  # type: ignore[assignment]
    stack = _restack_with(SlowBars())
    token = stack.login()
    with live_server(stack.app) as port:
        bars_headers = {**CONTRACT_HDR, **auth(token)}
        pool = ThreadPoolExecutor(max_workers=21)
        started = time.monotonic()
        bars = pool.submit(http_get, port, BARS_QUERY, bars_headers)
        time.sleep(0.2)  # let the bars request reach the blocked upstream call

        def health():
            t = time.monotonic()
            status, _h, _b = http_get(port, "/v1/health", dict(CONTRACT_HDR))
            return status, time.monotonic() - t

        results = [f.result() for f in [pool.submit(health) for _ in range(20)]]
        assert not bars.done(), "the bars call should still be blocked"
        assert all(status == 200 for status, _ in results)
        assert max(elapsed for _, elapsed in results) < 0.5
        status, _headers, _body = bars.result()
        assert status == 200
        assert time.monotonic() - started >= 1.0
        pool.shutdown()


def _restack_with(transport) -> Stack:
    """A Stack whose every component shares `transport` (the harness builds its own)."""
    from gateway_vm import app as relay_app
    from gateway_vm.breeze_client import BreezeClient
    from gateway_vm.egress import EgressChecker
    from gateway_vm.session import SessionManager

    stack = Stack()
    stack.transport = transport
    stack.breeze = BreezeClient(transport, clock=stack.clock)
    stack.egress = EgressChecker(
        transport, expected_ip=EXPECTED_IP, echo_url="https://api.ipify.org", clock=stack.clock
    )
    stack.session = SessionManager(
        clock=stack.clock,
        egress=stack.egress,
        breeze=stack.breeze,
        credentials=stack.credentials,
        expected_userid=USERID,
        events=stack.events,
        budget=stack.budget,
    )
    stack.app = relay_app.create_app(
        session=stack.session,
        breeze=stack.breeze,
        credentials=stack.credentials,
        budget=stack.budget,
        events=stack.events,
        clock=stack.clock,
    )
    stack.client = TestClient(stack.app, base_url=BASE)
    return stack


# ------------------------------------------------------ handlers_sync


def test_handlers_sync_every_route_endpoint_is_a_plain_function():
    stack = Stack(master_url="https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip")
    endpoints = [route for route in stack.app.routes if hasattr(route, "endpoint")]
    assert {route.path for route in endpoints} == set(contract.ROUTES)
    for route in endpoints:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path
        for dependency in route.dependant.dependencies:
            assert not inspect.iscoroutinefunction(dependency.call), (route.path, dependency.call)


NETWORK_MODULES = {"http.client", "urllib.request", "socket", "ssl", "httpx", "requests", "aiohttp", "urllib3"}
NETWORK_OWNERS = {"transport.py", "pool.py"}  # egress.py reaches the network only through a Transport


def network_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name in NETWORK_MODULES or a.name.split(".")[0] in {"socket", "ssl"}]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in NETWORK_MODULES or module.split(".")[0] in {"socket", "ssl", "httpx", "requests"}:
                found.append(module)
            if module == "http" and any(a.name == "client" for a in node.names):
                found.append("http.client")
            if module == "urllib" and any(a.name == "request" for a in node.names):
                found.append("urllib.request")
    return found


def test_handlers_sync_only_the_transports_import_a_network_library():
    checked = []
    for path in sorted(PKG.glob("*.py")):
        if path.name in NETWORK_OWNERS or path.name == "smoke_probe.py":
            continue
        checked.append(path.name)
        assert network_imports(path.read_text()) == [], path.name
    assert {"app.py", "session.py", "main.py", "secret_source.py", "budget.py", "egress.py", "breeze_client.py"} <= set(checked)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("import http.client\n", ["http.client"]),
        ("from http import client\n", ["http.client"]),
        ("import urllib.request\n", ["urllib.request"]),
        ("from urllib import request\n", ["urllib.request"]),
        ("import socket\n", ["socket"]),
        ("import ssl\n", ["ssl"]),
        ("import httpx\n", ["httpx"]),
        ("from urllib.parse import quote\n", []),
        ("import json\n", []),
    ],
)
def test_handlers_sync_scanner_flags_planted_network_imports(source, expected):
    assert network_imports(source) == expected


def test_a_colon_separated_state_directory_is_refused_loudly_with_exit_3(tmp_path, capsys):
    one, two = tmp_path / "a", tmp_path / "b"
    one.mkdir()
    two.mkdir()
    code, runner, transport, _ = run_main(tmp_path, environ={"STATE_DIRECTORY": f"{one}:{two}"})
    assert code == 3 and runner.servers == [] and transport.requests == []
    events = stdout_events(capsys)
    assert [(e["event"], e["result"], e["reason"]) for e in events] == [("service_start", "error", "config_invalid")]


def test_the_secret_reader_module_does_not_shadow_the_stdlib_secrets_module():
    assert not (PKG / "secrets.py").exists()
    assert (PKG / "secret_source.py").is_file()
