"""The IAP tunnel supervisor (contract growin-relay/1, C1; D-14, OD-3, OD-16).

The Mac reaches the relay only through `gcloud compute start-iap-tunnel`, run as
the daily identity (`--account=<daily account>`), listening on a random free
loopback port. The argv is a list and never goes through a shell; it never holds
ssh or the internal-IP flag. gcloud's stderr is discarded unless the operator
asks for it in their own terminal, because it can print project and host names.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from typing import Callable

from .config import GatewayConfig
from .errors import TunnelError

LOOPBACK = "127.0.0.1"
SSH_PORT = 22
STOP_GRACE_S = 5.0
POLL_INTERVAL_S = 0.25
CONNECT_PROBE_S = 1.0

_START_KEY = object()  # only Tunnel.start() holds this, so only it can build a TunnelHandle


def _plain(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("-") or any(
        c.isspace() or c == "\x00" for c in value
    ):
        raise ValueError(f"invalid tunnel argument: {name}")
    return value


def _port(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError(f"invalid tunnel argument: {name}")
    return value


def tunnel_argv(
    *,
    gcloud: str,
    instance: str,
    zone: str,
    project: str,
    account: str,
    local_port: int,
    relay_port: int,
) -> list[str]:
    """The exact C1 command as a list. relay_port 22 is refused."""
    if _port(relay_port, "relay_port") == SSH_PORT:
        raise ValueError("invalid tunnel argument: relay_port")
    return [
        _plain(gcloud, "gcloud"),
        "compute",
        "start-iap-tunnel",
        _plain(instance, "instance"),
        str(relay_port),
        f"--local-host-port={LOOPBACK}:{_port(local_port, 'local_port')}",
        f"--zone={_plain(zone, 'zone')}",
        f"--project={_plain(project, 'project')}",
        f"--account={_plain(account, 'account')}",
    ]


def pick_free_port() -> int:
    """A currently free loopback port. The tunnel child binds it a moment later."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def _can_connect(port: int) -> bool:
    try:
        with socket.create_connection((LOOPBACK, port), timeout=CONNECT_PROBE_S):
            return True
    except OSError:
        return False


def _signal_group(proc, killpg: Callable[[int, int], None], sig: int) -> None:
    try:
        killpg(proc.pid, sig)  # start_new_session makes the child its own group leader
    except (ProcessLookupError, PermissionError):
        pass


class TunnelHandle:
    """A running tunnel child. Built only by Tunnel.start()."""

    def __init__(
        self,
        proc,
        local_port: int,
        *,
        killpg: Callable[[int, int], None] = os.killpg,
        _key: object = None,
    ) -> None:
        if _key is not _START_KEY:
            raise TypeError("a TunnelHandle is created by Tunnel.start()")
        self._proc = proc
        self.local_port = local_port
        self._killpg = killpg
        self._stopped = False

    def alive(self) -> bool:
        return not self._stopped and self._proc.poll() is None

    def stop(self) -> None:
        """Terminate the group, wait 5 s, then kill it. Safe to call twice."""
        if self._stopped:
            return
        self._stopped = True
        if self._proc.poll() is not None:
            return
        _signal_group(self._proc, self._killpg, signal.SIGTERM)
        try:
            self._proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_group(self._proc, self._killpg, signal.SIGKILL)
            try:
                self._proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass


class Tunnel:
    """Starts one IAP tunnel per call to start(), always on a fresh random port."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        stderr_passthrough: bool = False,
        popen=subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
        connect: Callable[[int], bool] = _can_connect,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        pick_port: Callable[[], int] = pick_free_port,
        killpg: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self._config = config
        self._stderr_passthrough = stderr_passthrough
        self._popen = popen
        self._which = which
        self._connect = connect
        self._monotonic = monotonic
        self._sleep = sleep
        self._pick_port = pick_port
        self._killpg = killpg

    def start(self) -> TunnelHandle:
        gcloud = self._which("gcloud")
        if gcloud is None:
            raise TunnelError("gcloud_not_found")
        gcp = self._config.gcp
        local_port = self._pick_port()
        argv = tunnel_argv(
            gcloud=gcloud,
            instance=gcp.instance,
            zone=gcp.zone,
            project=gcp.project,
            account=gcp.daily_account,
            local_port=local_port,
            relay_port=self._config.tunnel.relay_port,
        )
        try:
            proc = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None if self._stderr_passthrough else subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),  # gcloud needs HOME and PATH; this is not a credential read
            )
        except FileNotFoundError:
            raise TunnelError("gcloud_not_found") from None
        except OSError:
            raise TunnelError("tunnel_spawn_failed") from None

        deadline = self._monotonic() + self._config.tunnel.ready_timeout_s
        while True:
            code = proc.poll()
            if code is not None:
                raise TunnelError("tunnel_exited", returncode=code)
            if self._connect(local_port):
                # Our own child must still be running: a squatter on the port would answer
                # while the child, unable to bind, is already gone.
                code = proc.poll()
                if code is not None:
                    raise TunnelError("tunnel_exited", returncode=code)
                return TunnelHandle(proc, local_port, killpg=self._killpg, _key=_START_KEY)
            if self._monotonic() >= deadline:
                _signal_group(proc, self._killpg, signal.SIGKILL)
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
                raise TunnelError("tunnel_timeout")
            self._sleep(POLL_INTERVAL_S)
