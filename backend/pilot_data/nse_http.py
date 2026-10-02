"""Paced, allow-listed HTTPS client for public NSE files.

Only https URLs on the two NSE hosts are accepted, redirects are refused, TLS
verification stays on (httpx default) and bodies are size capped. The headers are
ordinary browser headers for public data, not a bypass of any access control.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Literal
from urllib.parse import urlsplit

import httpx

from .core import ZIP_MAGIC, PilotDataError, SourceDescriptor, utc_now

ARCHIVES_HOST = "nsearchives.nseindia.com"
API_HOST = "www.nseindia.com"
ALLOWED_HOSTS = frozenset({ARCHIVES_HOST, API_HOST})
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://www.nseindia.com/"
ACCEPT_BY_EXPECT = {
    "zip": "*/*",
    "json": "application/json, text/plain, */*",
    "csv": "text/csv,*/*",
}
RETRY_SLEEPS = (2, 4)
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class FetchedFile:
    url: str
    status: int
    content: bytes
    content_type: str | None
    fetched_at: datetime

    def descriptor(self, kind: str, for_date: date | None) -> SourceDescriptor:
        host = urlsplit(self.url).hostname
        source: Literal["nse_archive", "nse_api"] = "nse_api" if host == API_HOST else "nse_archive"
        return SourceDescriptor(
            source=source, kind=kind, locator=self.url, fetched_at=self.fetched_at, for_date=for_date
        )


@dataclass(frozen=True)
class NoFile:
    url: str
    fetched_at: datetime


def build_default_client() -> httpx.Client:
    # TLS verification is left at the httpx default (on); redirects are never followed.
    return httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=False)


class NseHttp:
    def __init__(
        self,
        client: httpx.Client,
        *,
        min_interval_seconds: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._client = client
        self._min_interval = min_interval_seconds
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._max_bytes = max_bytes
        self._last_request: float | None = None

    def _pace(self) -> None:
        if self._last_request is None:
            return
        wait = self._last_request + self._min_interval - self._monotonic()
        if wait > 0:
            self._sleeper(wait)

    def _request(self, url: str, expect: str) -> tuple[int, bytes, str | None]:
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept": ACCEPT_BY_EXPECT[expect],
            "Referer": REFERER,
        }
        with self._client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            status = response.status_code
            content_type = response.headers.get("content-type")
            if 300 <= status < 400:
                raise PilotDataError("nse_redirect_refused", f"NSE answered with redirect status {status}")
            if status != 200:
                return status, b"", content_type
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self._max_bytes:
                raise PilotDataError("nse_response_too_large", "declared body is over the size cap")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > self._max_bytes:
                    raise PilotDataError("nse_response_too_large", "body is over the size cap")
            return status, bytes(body), content_type

    def fetch(self, url: str, *, expect: Literal["zip", "json", "csv"]) -> FetchedFile | NoFile:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise PilotDataError("nse_url_not_allowed", "only https URLs on the NSE hosts are allowed")
        if expect not in ACCEPT_BY_EXPECT:
            raise ValueError(f"unknown expectation {expect!r}")
        attempt = 0
        while True:
            self._pace()
            status: int | None
            try:
                status, body, content_type = self._request(url, expect)
            except httpx.HTTPError:
                status, body, content_type = None, b"", None
            self._last_request = self._monotonic()
            if status is None or status >= 500:
                if attempt < len(RETRY_SLEEPS):
                    self._sleeper(RETRY_SLEEPS[attempt])
                    attempt += 1
                    continue
                raise PilotDataError("nse_fetch_failed", f"NSE fetch failed after retries (status {status})")
            fetched_at = utc_now()
            if status == 404:
                return NoFile(url=url, fetched_at=fetched_at)
            if status != 200:
                raise PilotDataError("nse_fetch_refused", f"NSE refused the request with status {status}")
            self._validate(body, expect)
            return FetchedFile(
                url=url, status=status, content=body, content_type=content_type, fetched_at=fetched_at
            )

    @staticmethod
    def _validate(body: bytes, expect: str) -> None:
        ok = False
        if expect == "zip":
            ok = body.startswith(ZIP_MAGIC)
        elif expect == "json":
            try:
                ok = isinstance(json.loads(body), (dict, list))
            except (ValueError, UnicodeDecodeError):
                ok = False
        else:
            try:
                first_line = body.decode("utf-8-sig").splitlines()[0]
                ok = "," in first_line
            except (UnicodeDecodeError, IndexError):
                ok = False
        if not ok:
            raise PilotDataError("nse_unexpected_content", f"200 body does not look like {expect}")
