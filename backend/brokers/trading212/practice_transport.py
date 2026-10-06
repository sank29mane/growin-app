"""Host-locked HTTP transport for the Trading 212 practice account (66-03, D-13).

This module defines exactly one base URL, the demo host. A request event hook
refuses any request whose scheme is not https or whose host is not exactly
``demo.trading212.com``, redirects are never followed, and every request first
takes its endpoint-template slot from the governor (UKT-03, D-14).

Order traffic is deliberately narrow:

* ``POST /equity/orders/limit`` is the only POST. Its body must be a LIMIT DAY
  body (D-04); anything else is refused before a request exists.
* ``DELETE /equity/orders/{id}`` is the only DELETE.
* ``GET`` is limited to the read endpoints the adapter uses.

An order POST or DELETE is sent at most once. A GET that gets a 429 waits for
the governor and is sent once more, never a third time. No failure text here
carries a header, a key or a response body.

Time is read only through the injected clock and sleep (no ``random``, no
wall-clock call in this module). The credentials are handed in by the caller;
this module never reads the environment (D-07).
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Optional

import httpx

from .governor import Governor

DEMO_HOST = "demo.trading212.com"
DEMO_BASE_URL = f"https://{DEMO_HOST}/api/v0"
API_PREFIX = "/api/v0"

ORDER_LIMIT_PATH = "/equity/orders/limit"
_ORDER_ID_PREFIX = "/equity/orders/"

# The reads the practice adapter may issue. Anything else raises before a request.
_READ_PREFIXES = (
    "/equity/account/summary",
    "/equity/orders",
    "/equity/positions",
    "/equity/history/orders",
    "/equity/metadata/instruments",
    "/equity/metadata/exchanges",
)

# The one order body this module will POST (D-04).
LIMIT_BODY_KEYS = frozenset({"ticker", "quantity", "limitPrice", "timeValidity"})


class PracticeTransportError(Exception):
    """A request did not complete.

    ``kind`` is a stable word. ``sent`` is False only when nothing can have
    reached the broker (no connection was made, or a local guard refused the
    request first); True means the bytes may have left, so a POST outcome is
    unknown. The text never carries a header, a key or a body.
    """

    def __init__(self, kind: str, *, sent: bool) -> None:
        super().__init__(kind)
        self.kind = kind
        self.sent = sent

    def __str__(self) -> str:
        return self.kind


class PracticeHostRefused(PracticeTransportError):
    """The request hook refused a host, scheme, redirect or path. Nothing was sent."""

    def __init__(self, kind: str = "host_refused") -> None:
        super().__init__(kind, sent=False)


class PracticeRateLimited(PracticeTransportError):
    """A GET still got 429 after its single governed retry."""

    def __init__(self) -> None:
        super().__init__("rate_limited", sent=True)


@dataclass(frozen=True)
class _Credentials:
    """The Basic header value. Never shown by repr, str or an exception."""

    header: str = field(repr=False)

    def __repr__(self) -> str:
        return "_Credentials(<redacted>)"


def basic_header(key: str, secret: str) -> str:
    """``Basic base64(key:secret)``. Both parts are required (D-07)."""

    if not isinstance(key, str) or not key.strip():
        raise ValueError("practice key is required")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("practice secret is required")
    token = base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _relative(path: str) -> str:
    """Return the path below the API base, or raise. Absolute URLs are refused."""

    if not isinstance(path, str) or not path.startswith("/"):
        raise PracticeHostRefused("path_refused")
    if path.startswith(API_PREFIX + "/"):
        path = path[len(API_PREFIX) :]
    if "://" in path or path.startswith("//") or ".." in path.split("?", 1)[0]:
        raise PracticeHostRefused("path_refused")
    return path


def _bare(path: str) -> str:
    return path.split("?", 1)[0]


def validate_limit_body(body: Mapping[str, Any]) -> None:
    """Refuse anything but a LIMIT DAY body. Raises before any request exists (D-04)."""

    if set(body) != LIMIT_BODY_KEYS:
        raise PracticeHostRefused("order_body_refused")
    if body["timeValidity"] != "DAY":
        raise PracticeHostRefused("time_validity_refused")


class PracticeTransport:
    """One demo-account client. One governor, one credential, no redirects."""

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        governor: Optional[Governor] = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._credentials = _Credentials(basic_header(key, secret))
        self._clock = clock
        self._sleep = sleep
        self.governor = governor or Governor(clock=clock, sleep=sleep)
        self._client = httpx.AsyncClient(
            base_url=DEMO_BASE_URL,
            headers={
                "Authorization": self._credentials.header,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds / 2),
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [self._guard_request]},
        )

    def __repr__(self) -> str:
        return "PracticeTransport(host='demo.trading212.com')"

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    async def _guard_request(request: httpx.Request) -> None:
        url = request.url
        if url.scheme != "https" or url.host != DEMO_HOST or url.port not in (None, 443):
            raise PracticeHostRefused("host_refused")
        if not url.path.startswith(API_PREFIX + "/"):
            raise PracticeHostRefused("path_refused")

    # --- reads ---------------------------------------------------------------

    async def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> httpx.Response:
        """A governed GET. A 429 waits for the governor and is retried once."""

        relative = _relative(path)
        if not _bare(relative).startswith(_READ_PREFIXES):
            raise PracticeHostRefused("path_refused")
        response = await self._governed("GET", relative, params=params)
        if response.status_code == 429:
            key = self.governor.template("GET", relative)
            self.governor.hold_after_throttle(key, response.headers)
            response = await self._governed("GET", relative, params=params)
            if response.status_code == 429:
                raise PracticeRateLimited()
        return response

    # --- orders --------------------------------------------------------------

    async def post_limit_order(self, body: Mapping[str, Any]) -> httpx.Response:
        """One governed POST of a LIMIT DAY order. Never resent, whatever happens."""

        validate_limit_body(body)
        return await self._governed("POST", ORDER_LIMIT_PATH, json=dict(body))

    async def delete_order(self, order_id: int) -> httpx.Response:
        """One governed DELETE for one order id. Never resent."""

        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            raise PracticeHostRefused("order_id_refused")
        return await self._governed("DELETE", f"{_ORDER_ID_PREFIX}{order_id}")

    # --- internals -----------------------------------------------------------

    async def _governed(
        self,
        method: str,
        relative: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
    ) -> httpx.Response:
        key = await self.governor.acquire(method, relative)
        try:
            response = await self._client.request(method, relative, params=params, json=json)
        except PracticeTransportError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise PracticeTransportError("connect_failed", sent=False) from None
        except httpx.TimeoutException:
            raise PracticeTransportError("timeout", sent=True) from None
        except httpx.HTTPError:
            raise PracticeTransportError("transport_error", sent=True) from None
        except Exception:
            # Anything unexpected after a send began is treated as unknown, never as a refusal.
            raise PracticeTransportError("unexpected_error", sent=True) from None
        self.governor.observe(key, response.headers)
        if response.is_redirect:
            # follow_redirects is off, so the 30x is the answer. It is refused,
            # never followed and never treated as success.
            raise PracticeTransportError("redirect_refused", sent=True)
        return response
