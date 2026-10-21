"""Egress check (D-13): the VM must leave from the IP registered with the broker.

Both the instance metadata NAT IP and a public echo service must equal the
expected IP. The result holds booleans and a timestamp only, never an IP
string.
"""

from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass
from datetime import datetime

from .transport import Clock, Timeouts, Transport

METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "network-interfaces/0/access-configs/0/external-ip"
)
_METADATA_TIMEOUTS = Timeouts(connect_s=2.0, read_s=3.0, total_s=3.0)
_ECHO_TIMEOUTS = Timeouts(connect_s=5.0, read_s=6.0, total_s=6.0)
_MAX_BYTES = 256
FAILED_RECHECK_COOLDOWN_S = 30.0


@dataclass(frozen=True)
class EgressResult:
    ok: bool
    metadata_match: bool | None
    echo_match: bool | None
    checked_at_utc: datetime


class EgressChecker:
    """Two network fetches per check. A failing check is not repeated for a while.

    After a failed result, calls inside `failed_recheck_cooldown_s` return that
    same failed result without any request, so a broken echo service or an IP
    mismatch cannot cost a probe per incoming request. Only a failure is ever
    reused: a success is never cached here (the session decides how long one
    stays fresh), so this cannot turn a failed or stale result into a pass.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        expected_ip: str,
        echo_url: str,
        clock: Clock,
        failed_recheck_cooldown_s: float = FAILED_RECHECK_COOLDOWN_S,
    ) -> None:
        self._transport = transport
        self._expected_ip = expected_ip
        self._echo_url = echo_url
        self._clock = clock
        self._cooldown_s = failed_recheck_cooldown_s
        self._lock = threading.Lock()
        self._last_failed: EgressResult | None = None
        self._last_failed_mono: float | None = None

    def _fetch_ip(self, url: str, headers: dict[str, str], timeouts: Timeouts):
        resp = self._transport.request(
            "GET", url, headers=headers, body=None, timeouts=timeouts, max_bytes=_MAX_BYTES
        )
        if resp.status != 200:
            raise ValueError("bad status")
        return ipaddress.ip_address(resp.body.decode("ascii").strip())

    def check(self) -> EgressResult:
        """Never raises: any fetch or parse failure gives ok=False."""
        return self.check_reporting_cache()[0]

    def check_reporting_cache(self, *, keep_failure_on_ok: bool = False) -> tuple[EgressResult, bool]:
        """Like check(), and True when the result is a reused failure that made no request.

        By default an ok clears the failure cooldown. The session passes
        keep_failure_on_ok=True and calls clear_failure_before() only when it
        commits the ok, so an ok it drops cannot end the cooldown of a failure
        that outranks it.
        """
        with self._lock:
            cached, since = self._last_failed, self._last_failed_mono
        if (
            cached is not None
            and since is not None
            and 0 <= self._clock.monotonic() - since < self._cooldown_s
        ):
            return cached, True
        result = self._check_now()
        with self._lock:
            if result.ok:
                if not keep_failure_on_ok:
                    self._last_failed, self._last_failed_mono = None, None
            else:
                self._last_failed, self._last_failed_mono = result, self._clock.monotonic()
        return result, False

    def clear_failure_before(self, started_mono: float) -> None:
        """End the cooldown of a failure recorded before `started_mono`; a newer one stays."""
        with self._lock:
            if self._last_failed_mono is not None and self._last_failed_mono < started_mono:
                self._last_failed, self._last_failed_mono = None, None

    def _check_now(self) -> EgressResult:
        try:
            expected = ipaddress.ip_address(str(self._expected_ip).strip())
        except Exception:
            return EgressResult(False, None, None, self._clock.now_utc())

        def side(url: str, headers: dict[str, str], timeouts: Timeouts) -> bool | None:
            try:
                return self._fetch_ip(url, headers, timeouts) == expected
            except Exception:
                return None

        metadata = side(METADATA_URL, {"Metadata-Flavor": "Google"}, _METADATA_TIMEOUTS)
        echo = side(self._echo_url, {}, _ECHO_TIMEOUTS)
        return EgressResult(
            ok=metadata is True and echo is True,
            metadata_match=metadata,
            echo_match=echo,
            checked_at_utc=self._clock.now_utc(),
        )
