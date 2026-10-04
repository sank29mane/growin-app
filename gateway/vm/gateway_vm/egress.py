"""Egress check (D-13): the VM must leave from the IP registered with the broker.

Both the instance metadata NAT IP and a public echo service must equal the
expected IP. The result holds booleans and a timestamp only, never an IP
string.
"""

from __future__ import annotations

import ipaddress
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


@dataclass(frozen=True)
class EgressResult:
    ok: bool
    metadata_match: bool | None
    echo_match: bool | None
    checked_at_utc: datetime


class EgressChecker:
    def __init__(
        self, transport: Transport, *, expected_ip: str, echo_url: str, clock: Clock
    ) -> None:
        self._transport = transport
        self._expected_ip = expected_ip
        self._echo_url = echo_url
        self._clock = clock

    def _fetch_ip(self, url: str, headers: dict[str, str], timeouts: Timeouts):
        resp = self._transport.request(
            "GET", url, headers=headers, body=None, timeouts=timeouts, max_bytes=_MAX_BYTES
        )
        if resp.status != 200:
            raise ValueError("bad status")
        return ipaddress.ip_address(resp.body.decode("ascii").strip())

    def check(self) -> EgressResult:
        """Never raises: any fetch or parse failure gives ok=False."""
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
