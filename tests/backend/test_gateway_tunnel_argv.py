"""IAP tunnel supervisor, strict Mac config and RelayClient.open() (Phase 61-09).

No test runs gcloud, opens a socket or starts a process: Popen, which, connect,
the clock and the port picker are all injected fakes.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
from pathlib import Path

import httpx
import pytest

from test_gateway_relay_client import HEALTH_JSON, Canned, json_response, mock_http

from brokers.breeze_gateway import RelayClient, RelayUnavailable, TunnelError
from brokers.breeze_gateway.config import (
    ConfigError,
    GatewayConfig,
    load_gateway_config,
    parse_gateway_config,
)
from brokers.breeze_gateway.tunnel import STOP_GRACE_S, Tunnel, TunnelHandle, pick_free_port, tunnel_argv

ROOT = Path(__file__).resolve().parents[2]

VALID = {
    "gcp": {
        "project": "proj-12345",
        "zone": "asia-south1-c",
        "instance": "relay-vm",
        "daily_account": "daily@example.com",
    },
    "tunnel": {"relay_port": 8443, "ready_timeout_s": 30},
    "listener": {"window_s": 120},
}
GCLOUD = "/usr/local/bin/gcloud"
EXPECTED_ARGV = [
    GCLOUD,
    "compute",
    "start-iap-tunnel",
    "relay-vm",
    "8443",
    "--local-host-port=127.0.0.1:50123",
    "--zone=asia-south1-c",
    "--project=proj-12345",
    "--account=daily@example.com",
]


def make_config(**overrides) -> GatewayConfig:
    data = copy.deepcopy(VALID)
    for section, values in overrides.items():
        data[section].update(values)
    return parse_gateway_config(data)


# ------------------------------------------------------------------ argv


def argv(**over):
    base = dict(
        gcloud=GCLOUD,
        instance="relay-vm",
        zone="asia-south1-c",
        project="proj-12345",
        account="daily@example.com",
        local_port=50123,
        relay_port=8443,
    )
    base.update(over)
    return tunnel_argv(**base)


def test_the_argv_is_exactly_the_c1_command():
    assert argv() == EXPECTED_ARGV


def test_the_argv_never_holds_ssh_internal_ip_a_shell_or_another_host():
    words = argv()
    assert isinstance(words, list) and all(isinstance(w, str) for w in words)
    text = " ".join(words)
    for forbidden in ("ssh", "--internal-ip", "--tunnel-through-iap", "sh -c", "0.0.0.0", "localhost"):
        assert forbidden not in text
    assert [w for w in words if "127.0.0.1" in w] == ["--local-host-port=127.0.0.1:50123"]
    assert words[-1].startswith("--account=")  # the daily identity is always pinned


@pytest.mark.parametrize("port", [22, 0, 65536, -1, True, "8443"])
def test_a_bad_relay_port_is_refused(port):
    with pytest.raises(ValueError):
        argv(relay_port=port)


@pytest.mark.parametrize("port", [0, 65536, True, "1"])
def test_a_bad_local_port_is_refused(port):
    with pytest.raises(ValueError):
        argv(local_port=port)


@pytest.mark.parametrize("name", ["gcloud", "instance", "zone", "project", "account"])
@pytest.mark.parametrize("value", ["--impersonate-service-account=x", "-x", "a b", "", "a\x00b"])
def test_an_argument_that_could_start_a_flag_or_split_is_refused(name, value):
    with pytest.raises(ValueError):
        argv(**{name: value})


def test_pick_free_port_returns_a_loopback_port_that_can_be_bound():
    import socket

    port = pick_free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


# ------------------------------------------------------------- the tunnel


class FakeProc:
    def __init__(self, pid: int = 4242, *, exits_at_poll: int | None = None, code: int = 1,
                 hangs: bool = False) -> None:
        self.pid = pid
        self.polls = 0
        self.exits_at_poll = exits_at_poll
        self.code = code
        self.hangs = hangs
        self.dead = False
        self.waits: list[float] = []

    def poll(self):
        self.polls += 1
        if self.dead:
            return self.code
        if self.exits_at_poll is not None and self.polls >= self.exits_at_poll:
            return self.code
        return None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.hangs and not self.dead:
            self.hangs = False  # the next wait (after SIGKILL) returns
            raise subprocess.TimeoutExpired("fake", timeout)
        self.dead = True
        return self.code


class Rig:
    """Injected collaborators for a Tunnel."""

    def __init__(self, procs, connects=(True,), ports=(50123, 50124, 50125)) -> None:
        self.procs = list(procs)
        self.popen_calls: list[tuple[list, dict]] = []
        self.killpg_calls: list[tuple[int, int]] = []
        self.sleeps: list[float] = []
        self.now = 0.0
        self.connects = list(connects)
        self.connect_ports: list[int] = []
        self.ports = list(ports)
        self.gcloud: str | None = GCLOUD

    def popen(self, words, **kwargs):
        self.popen_calls.append((words, kwargs))
        return self.procs.pop(0)

    def which(self, name):
        assert name == "gcloud"
        return self.gcloud

    def connect(self, port):
        self.connect_ports.append(port)
        return self.connects.pop(0) if len(self.connects) > 1 else self.connects[0]

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def pick(self):
        return self.ports.pop(0)

    def killpg(self, pgid, sig):
        self.killpg_calls.append((pgid, sig))

    def tunnel(self, config=None, **extra) -> Tunnel:
        wiring = dict(
            popen=self.popen,
            which=self.which,
            connect=self.connect,
            monotonic=self.monotonic,
            sleep=self.sleep,
            pick_port=self.pick,
            killpg=self.killpg,
        )
        wiring.update(extra)
        return Tunnel(config or make_config(), **wiring)


def test_start_runs_the_exact_argv_as_a_list_without_a_shell():
    rig = Rig([FakeProc()], connects=[False, False, True])
    handle = rig.tunnel().start()
    assert handle.local_port == 50123 and handle.alive()
    words, kwargs = rig.popen_calls[0]
    assert words == EXPECTED_ARGV
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert not kwargs.get("shell")
    assert isinstance(kwargs["env"], dict) and kwargs["env"] == dict(os.environ)
    assert kwargs["env"] is not os.environ  # a copy
    assert rig.connect_ports == [50123, 50123, 50123]  # only ever its own port
    assert len(rig.sleeps) == 2


def test_each_start_picks_a_fresh_port():
    rig = Rig([FakeProc(pid=1), FakeProc(pid=2)])
    tunnel = rig.tunnel()
    first, second = tunnel.start(), tunnel.start()
    assert (first.local_port, second.local_port) == (50123, 50124)
    assert rig.popen_calls[0][0][5].endswith(":50123")
    assert rig.popen_calls[1][0][5].endswith(":50124")


def test_stderr_goes_to_the_terminal_only_when_asked():
    rig = Rig([FakeProc()])
    rig.tunnel(stderr_passthrough=True).start()
    assert rig.popen_calls[0][1]["stderr"] is None


def test_a_child_that_exits_first_is_tunnel_exited_with_no_stderr_text():
    rig = Rig([FakeProc(exits_at_poll=1, code=7)], connects=[False])
    with pytest.raises(TunnelError) as info:
        rig.tunnel().start()
    assert info.value.code == "tunnel_exited" and info.value.returncode == 7
    assert str(info.value) == "tunnel_exited"
    assert rig.connect_ports == []  # never talks to a port whose child is gone


def test_a_squatter_answering_for_a_dead_child_is_refused():
    # the port accepts, but the child exited between the first poll and the answer
    rig = Rig([FakeProc(exits_at_poll=2, code=1)], connects=[True])
    with pytest.raises(TunnelError) as info:
        rig.tunnel().start()
    assert info.value.code == "tunnel_exited"


def test_a_tunnel_that_never_listens_is_killed_after_the_ready_timeout():
    proc = FakeProc(pid=777)
    rig = Rig([proc], connects=[False])
    with pytest.raises(TunnelError) as info:
        rig.tunnel(make_config(tunnel={"ready_timeout_s": 3})).start()
    assert info.value.code == "tunnel_timeout"
    assert rig.killpg_calls == [(777, signal.SIGKILL)]
    assert rig.now >= 3


class NotReady(OSError):
    """An error that is not the expected not-ready answer of a connect probe."""


def test_an_interrupt_while_waiting_kills_the_child_and_propagates():
    proc = FakeProc(pid=778)
    rig = Rig([proc], connects=[False])

    def interrupted(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        rig.tunnel(sleep=interrupted).start()
    assert rig.killpg_calls == [(778, signal.SIGKILL)]
    assert proc.waits == [STOP_GRACE_S]


def test_an_unexpected_connect_error_kills_the_child_and_propagates():
    proc = FakeProc(pid=779)
    rig = Rig([proc])
    boom = NotReady("probe failed")

    def broken(port):
        raise boom

    with pytest.raises(NotReady) as info:
        rig.tunnel(connect=broken).start()
    assert info.value is boom
    assert rig.killpg_calls == [(779, signal.SIGKILL)]


def test_the_timeout_and_exit_paths_do_not_kill_twice():
    rig = Rig([FakeProc(pid=780)], connects=[False])
    with pytest.raises(TunnelError):
        rig.tunnel(make_config(tunnel={"ready_timeout_s": 1})).start()
    assert rig.killpg_calls == [(780, signal.SIGKILL)]
    rig = Rig([FakeProc(pid=781, exits_at_poll=1, code=2)], connects=[False])
    with pytest.raises(TunnelError):
        rig.tunnel().start()
    assert rig.killpg_calls == []


def test_a_missing_gcloud_is_reported_before_anything_runs():
    rig = Rig([FakeProc()])
    rig.gcloud = None
    with pytest.raises(TunnelError) as info:
        rig.tunnel().start()
    assert info.value.code == "gcloud_not_found"
    assert rig.popen_calls == []


def test_a_popen_failure_is_a_tunnel_error():
    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, *a, **k):
            raise self.exc

    for exc, code in ((FileNotFoundError("x"), "gcloud_not_found"), (PermissionError("x"), "tunnel_spawn_failed")):
        rig = Rig([])
        tunnel = Tunnel(make_config(), popen=Boom(exc), which=rig.which, connect=rig.connect,
                        monotonic=rig.monotonic, sleep=rig.sleep, pick_port=rig.pick)
        with pytest.raises(TunnelError) as info:
            tunnel.start()
        assert info.value.code == code


def test_stop_terminates_waits_five_seconds_then_kills_the_group():
    proc = FakeProc(pid=900, hangs=True)
    rig = Rig([proc])
    handle = rig.tunnel().start()
    handle.stop()
    assert rig.killpg_calls == [(900, signal.SIGTERM), (900, signal.SIGKILL)]
    assert proc.waits[0] == 5.0
    assert handle.alive() is False
    handle.stop()  # twice is fine
    assert len(rig.killpg_calls) == 2


def test_stop_does_not_kill_when_the_child_exits_on_terminate():
    proc = FakeProc(pid=901)
    rig = Rig([proc])
    handle = rig.tunnel().start()
    handle.stop()
    assert rig.killpg_calls == [(901, signal.SIGTERM)]
    assert handle.alive() is False


def test_alive_is_false_after_stop_even_if_the_child_ignores_both_signals():
    class Stubborn(FakeProc):
        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("fake", timeout)

    proc = Stubborn(pid=902)
    rig = Rig([proc])
    handle = rig.tunnel().start()
    assert handle.alive()
    handle.stop()
    assert handle.alive() is False  # a stopped handle is never reused


def test_alive_is_false_once_the_child_has_exited():
    proc = FakeProc()
    handle = Rig([proc]).tunnel().start()
    assert handle.alive()
    proc.dead = True
    assert not handle.alive()


def test_a_tunnel_handle_cannot_be_built_by_hand():
    with pytest.raises(TypeError):
        TunnelHandle(FakeProc(), 50123)


# ---------------------------------------------- the client on a tunnel


class FakeHandle:
    def __init__(self, port: int, *, alive: bool = True) -> None:
        self.local_port = port
        self._alive = alive
        self.stops = 0

    def alive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self.stops += 1
        self._alive = False


class FakeTunnel:
    def __init__(self, ports=(50123, 50124, 50125), fail_on=()) -> None:
        self.ports = list(ports)
        self.handles: list[FakeHandle] = []
        self.starts = 0
        self.fail_on = set(fail_on)

    def start(self):
        self.starts += 1
        if self.starts in self.fail_on:
            raise TunnelError("tunnel_exited", returncode=1)
        handle = FakeHandle(self.ports.pop(0))
        self.handles.append(handle)
        return handle


class Factory:
    """http_factory seam: records the ports and serves canned answers."""

    def __init__(self, handler=None) -> None:
        self.ports: list[int] = []
        self.handler = handler or Canned()
        self.clients: list[httpx.Client] = []

    def __call__(self, port: int) -> httpx.Client:
        self.ports.append(port)
        client = mock_http(self.handler)
        self.clients.append(client)
        return client


def test_a_client_on_a_dead_handle_refuses_to_send():
    seen = Canned()
    relay = RelayClient(tunnel=FakeHandle(50123, alive=False), http_factory=lambda port: mock_http(seen))
    with pytest.raises(RelayUnavailable) as info:
        relay.health()
    assert info.value.reason == "tunnel_down"
    assert seen.requests == []


def test_the_client_never_restarts_a_handle_it_was_given():
    factory = Factory()
    handle = FakeHandle(50123)
    relay = RelayClient(tunnel=handle, http_factory=factory)
    handle._alive = False
    with pytest.raises(RelayUnavailable):
        relay.health()
    assert factory.ports == [50123]
    relay.close()
    assert handle.stops == 0  # not ours to stop


def test_open_retries_health_until_the_relay_answers_and_close_stops_the_tunnel_once():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) < 3:
            raise httpx.ConnectError("not yet", request=request)
        return json_response(HEALTH_JSON)

    supervisor, factory = FakeTunnel(), Factory(handler)
    sleeps: list[float] = []
    relay = RelayClient.open(
        make_config(), tunnel=supervisor, http_factory=factory, sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )
    assert attempts == ["/v1/health"] * 3 and len(sleeps) == 2
    assert supervisor.starts == 1 and factory.ports == [50123]
    relay.close()
    relay.close()
    assert supervisor.handles[0].stops == 1


def test_open_gives_up_after_the_ready_timeout_and_stops_the_tunnel():
    clock = iter(range(0, 1000, 10))

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("never", request=request)

    supervisor = FakeTunnel()
    with pytest.raises(RelayUnavailable) as info:
        RelayClient.open(
            make_config(), tunnel=supervisor, http_factory=Factory(handler),
            sleep=lambda s: None, monotonic=lambda: next(clock),
        )
    assert info.value.reason == "relay_not_ready"
    assert supervisor.handles[0].stops == 1


def test_open_passes_a_tunnel_error_through_unchanged():
    supervisor = FakeTunnel(fail_on={1})
    with pytest.raises(TunnelError) as info:
        RelayClient.open(make_config(), tunnel=supervisor, http_factory=Factory())
    assert info.value.code == "tunnel_exited"


def test_open_does_not_return_a_client_whose_child_died_during_the_health_check():
    supervisor = FakeTunnel()

    def handler(request: httpx.Request) -> httpx.Response:
        supervisor.handles[0]._alive = False  # the child is gone as the answer arrives
        return json_response(HEALTH_JSON)

    with pytest.raises(RelayUnavailable):
        RelayClient.open(make_config(), tunnel=supervisor, http_factory=Factory(handler))
    assert supervisor.handles[0].stops == 1


def test_open_only_talks_to_the_port_its_own_tunnel_opened():
    supervisor = FakeTunnel(ports=(51000,))
    factory = Factory()
    relay = RelayClient.open(make_config(), tunnel=supervisor, http_factory=factory)
    assert factory.ports == [51000]
    relay.close()


def test_a_dead_child_gets_one_replacement_tunnel_on_a_new_port_and_the_request_goes_through():
    supervisor, factory = FakeTunnel(), Factory()
    relay = RelayClient.open(make_config(), tunnel=supervisor, http_factory=factory)
    first = supervisor.handles[0]
    first._alive = False  # IAP dropped the idle session
    assert relay.health().state == "none"
    assert supervisor.starts == 2
    assert factory.ports == [50123, 50124]
    assert first.stops == 1 and supervisor.handles[1].alive()
    relay.health()
    assert supervisor.starts == 2  # still alive: nothing restarted
    relay.close()
    assert supervisor.handles[1].stops == 1


def test_a_failed_replacement_is_relay_unavailable():
    supervisor, factory = FakeTunnel(fail_on={2}), Factory()
    relay = RelayClient.open(make_config(), tunnel=supervisor, http_factory=factory)
    supervisor.handles[0]._alive = False
    with pytest.raises(RelayUnavailable) as info:
        relay.health()
    assert info.value.reason == "tunnel_restart_failed"
    assert supervisor.starts == 2


def test_a_request_that_failed_mid_flight_is_never_resent_and_restarts_nothing():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return json_response(HEALTH_JSON)
        raise httpx.ReadError("dropped", request=request)

    supervisor = FakeTunnel()
    relay = RelayClient.open(make_config(), tunnel=supervisor, http_factory=Factory(handler))
    with pytest.raises(RelayUnavailable):
        relay.budget()
    assert calls == ["/v1/health", "/v1/budget"]
    assert supervisor.starts == 1


def test_the_default_http_client_is_built_on_the_handles_loopback_port():
    supervisor = FakeTunnel(ports=(50999,))
    relay = RelayClient(tunnel=supervisor.start(), restart=supervisor.start)
    try:
        assert str(relay._http.base_url) == "http://127.0.0.1:50999"
    finally:
        relay.close()


# ----------------------------------------------------------------- config


def write_config(tmp_path: Path, data, mode: int = 0o600) -> Path:
    path = tmp_path / "gateway.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(mode)
    return path


def reject(tmp_path: Path, data, code: str | None = None, mode: int = 0o600) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load_gateway_config(write_config(tmp_path, data, mode))
    if code is not None:
        assert info.value.code == code
    return info.value


def with_value(section: str, key: str, value):
    data = copy.deepcopy(VALID)
    data[section][key] = value
    return data


def test_a_valid_config_loads(tmp_path):
    config = load_gateway_config(write_config(tmp_path, VALID))
    assert config.gcp.project == "proj-12345" and config.gcp.zone == "asia-south1-c"
    assert config.gcp.instance == "relay-vm" and config.gcp.daily_account == "daily@example.com"
    assert (config.tunnel.relay_port, config.tunnel.ready_timeout_s) == (8443, 30)
    assert config.listener is not None and config.listener.window_s == 120
    assert load_gateway_config(write_config(tmp_path, VALID, 0o400)) == config


def test_the_listener_section_is_optional(tmp_path):
    data = copy.deepcopy(VALID)
    del data["listener"]
    assert load_gateway_config(write_config(tmp_path, data)).listener is None


def test_the_example_file_is_refused_until_it_is_filled_in(tmp_path):
    example = json.loads((ROOT / "gateway" / "ops" / "gateway.example.json").read_text())
    assert reject(tmp_path, example, "placeholder")
    filled = json.loads(json.dumps(example))
    filled["gcp"] = dict(VALID["gcp"])
    assert load_gateway_config(write_config(tmp_path, filled)).gcp.instance == "relay-vm"


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("gcp", "project", "Proj-12345"),
        ("gcp", "project", "p1"),
        ("gcp", "project", "-proj-12345"),
        ("gcp", "project", "proj-12345\n"),
        ("gcp", "zone", "asia-south1"),
        ("gcp", "zone", "--zone-evil1-a"),
        ("gcp", "instance", "-relay-vm"),
        ("gcp", "instance", "Relay"),
        ("gcp", "instance", "relay vm"),
        ("gcp", "daily_account", "-daily@example.com"),
        ("gcp", "daily_account", "daily@example"),
        ("gcp", "daily_account", "daily@example.com --impersonate"),
        ("gcp", "daily_account", ""),
        ("gcp", "project", 12345),
        ("gcp", "zone", None),
        ("tunnel", "relay_port", 22),
        ("tunnel", "relay_port", 0),
        ("tunnel", "relay_port", 65536),
        ("tunnel", "relay_port", "8443"),
        ("tunnel", "relay_port", True),
        ("tunnel", "relay_port", 8443.0),
        ("tunnel", "ready_timeout_s", 0),
        ("tunnel", "ready_timeout_s", 301),
        ("listener", "window_s", 121),
        ("listener", "window_s", 0),
    ],
)
def test_a_bad_value_is_refused(tmp_path, section, key, value):
    error = reject(tmp_path, with_value(section, key, value))
    assert error.key == f"{section}.{key}"


@pytest.mark.parametrize("placeholder", ["<GCP_PROJECT_ID>", "proj-<X>-1234"])
def test_placeholders_are_refused(tmp_path, placeholder):
    reject(tmp_path, with_value("gcp", "project", placeholder), "placeholder")


def test_structure_is_strict(tmp_path):
    reject(tmp_path, {**VALID, "extra": 1}, "unknown_key")
    reject(tmp_path, with_value("gcp", "admin_account", "x@example.com"), "unknown_key")
    reject(tmp_path, with_value("tunnel", "host", "x"), "unknown_key")
    reject(tmp_path, with_value("listener", "port", 443), "unknown_key")
    for section in ("gcp", "tunnel"):
        data = copy.deepcopy(VALID)
        del data[section]
        reject(tmp_path, data, "missing_section")
    for key in VALID["gcp"]:
        data = copy.deepcopy(VALID)
        del data["gcp"][key]
        error = reject(tmp_path, data, "missing_key")
        assert error.key == f"gcp.{key}"  # a missing daily_account is refused, never defaulted
    reject(tmp_path, [], "bad_type")
    reject(tmp_path, "{not json", "bad_json")
    reject(tmp_path, "", "bad_json")
    reject(tmp_path, '{"gcp": {"project": "a"}, "gcp": {}}', "duplicate_key")


def test_file_mode_owner_and_kind_are_checked(tmp_path):
    reject(tmp_path, VALID, "bad_mode", mode=0o644)
    reject(tmp_path, VALID, "bad_mode", mode=0o660)
    reject(tmp_path, VALID, "bad_mode", mode=0o700)
    real = write_config(tmp_path, VALID)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ConfigError) as info:
        load_gateway_config(link)
    assert info.value.code == "unreadable"
    with pytest.raises(ConfigError):
        load_gateway_config(tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        load_gateway_config(tmp_path)  # a directory


def test_a_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    path = write_config(tmp_path, VALID)
    assert load_gateway_config(path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ConfigError) as info:
        load_gateway_config(path)
    assert info.value.code == "bad_owner"


def test_an_oversized_file_is_refused(tmp_path):
    reject(tmp_path, '{"pad": "' + "x" * 20000 + '"}', "too_large")


def test_config_errors_never_carry_a_value(tmp_path):
    canary = "CANARY-VALUE-DO-NOT-ECHO"
    for data in (
        with_value("gcp", "zone", canary),
        with_value("gcp", "project", canary),
        with_value("gcp", "daily_account", canary + "@x.com extra"),
        {**VALID, canary: 1},
        with_value("gcp", canary, "v"),
    ):
        error = reject(tmp_path, data)
        assert canary not in str(error) and canary not in error.key and canary not in repr(error)
