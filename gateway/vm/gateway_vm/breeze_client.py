"""Thin read-only client for the broker's REST API (stdlib only).

Own client with the documented checksum, no vendor SDK (OD-2). Every response
passes through one gate, normalize() or classify_payload(): the JSON Status is
not the HTTP status, so HTTP 200 with Status != 200, a non-empty Error, or a
null or empty-string Success is an error (fail closed).

There is no order code path: no function and no endpoint constant here places,
modifies or cancels an order. Phase 63 owns order relay.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlencode

from .transport import Clock, HttpResponse, Timeouts, Transport, TransportError

SDK_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_5_8) AppleWebKit/534.50.2 "
    "(KHTML, like Gecko) Version/5.0.6 Safari/533.22.3"
)
V1_BASE = "https://api.icicidirect.com/breezeapi/api/v1/"
V2_HISTORICAL_URL = "https://breezeapi.icicidirect.com/api/v2/historicalcharts"
V1_ENDPOINTS = frozenset({"customerdetails", "quotes", "preview_order"})
V2_DAILY_INTERVAL = "1day"  # 61-05 may change this from the smoke result
ERROR_CLASSES = (
    "invalid_session",
    "resource_not_available",
    "public_key_missing",
    "ip_mismatch",
    "no_data",
    "other",
)

JSON_MAX_BYTES = 8 * 1024 * 1024
_SEGMENT_KEYS = ("Trading", "Equity", "Derivatives", "Currency")
_PRICE_RE = re.compile(r"^\d+(\.\d{1,2})?$")


# ---------------------------------------------------------------- helpers


def compact_json(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"))


def breeze_timestamp(now_utc: datetime) -> str:
    """UTC isoformat truncated to whole seconds, with a fixed .000Z suffix."""
    if now_utc.tzinfo is None:
        raise ValueError("timestamp needs a tz-aware datetime")
    return now_utc.astimezone(timezone.utc).isoformat()[:19] + ".000Z"


def checksum(timestamp: str, body: str, secret_key: str) -> str:
    return hashlib.sha256((timestamp + body + secret_key).encode("utf-8")).hexdigest()


def checksum_headers(
    *,
    app_key: str,
    secret_key: str,
    session_token: str,
    body: str,
    now_utc: datetime,
) -> dict[str, str]:
    ts = breeze_timestamp(now_utc)
    return {
        "Content-Type": "application/json",
        "X-Checksum": "token " + checksum(ts, body, secret_key),
        "X-Timestamp": ts,
        "X-AppKey": app_key,
        "X-SessionToken": session_token,
    }


def classify_error_text(text: object) -> str:
    if not isinstance(text, str):
        return "other"
    if text == "Invalid session.":
        return "invalid_session"
    if text == "Resource not available.":
        return "resource_not_available"
    if text == "Public Key does not exist.":
        return "public_key_missing"
    if "IP address used does not match" in text:
        return "ip_mismatch"
    if "no data" in text.lower():
        return "no_data"
    return "other"


class BreezeError(Exception):
    """code: UPSTREAM_ERROR | UPSTREAM_SHAPE | UPSTREAM_TIMEOUT | UPSTREAM_UNREACHABLE.

    str(err) is only f"{code}:{error_class}". The raw Error text is never kept.
    """

    def __init__(
        self,
        code: str,
        *,
        breeze_status: int | None = None,
        error_class: str = "other",
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.breeze_status = breeze_status
        self.error_class = error_class
        self.http_status = http_status
        super().__init__(f"{code}:{error_class}")

    def __str__(self) -> str:
        return f"{self.code}:{self.error_class}"


@dataclass(frozen=True)
class PayloadVerdict:
    ok: bool
    breeze_status: int | None
    error_class: str | None


_BAD = object()


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _evaluate(http_status: int, body: bytes) -> tuple[object, str, PayloadVerdict]:
    """Shared rules. Returns (success_or__BAD, error_code, verdict)."""
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return _BAD, "UPSTREAM_SHAPE", PayloadVerdict(False, None, "other")
    if not isinstance(payload, dict):
        return _BAD, "UPSTREAM_SHAPE", PayloadVerdict(False, None, "other")
    status = payload.get("Status")
    error = payload.get("Error")
    success = payload.get("Success")
    good = (
        http_status == 200
        and _int_or_none(status) == 200
        and (error is None or error == "")
        and success is not None
        and success != ""
    )
    if good:
        return success, "", PayloadVerdict(True, 200, None)
    return (
        _BAD,
        "UPSTREAM_ERROR",
        PayloadVerdict(False, _int_or_none(status), classify_error_text(error)),
    )


def normalize(http_status: int, body: bytes) -> object:
    """Return payload["Success"] or raise BreezeError."""
    success, code, verdict = _evaluate(http_status, body)
    if success is _BAD:
        raise BreezeError(
            code,
            breeze_status=verdict.breeze_status,
            error_class=verdict.error_class or "other",
            http_status=http_status,
        )
    return success


def classify_payload(http_status: int, body: bytes) -> PayloadVerdict:
    """Same rules as normalize, but never raises."""
    return _evaluate(http_status, body)[2]


@dataclass(frozen=True)
class RawUpstream:
    http_status: int
    body: bytes
    fetched_at_utc: datetime


@dataclass(frozen=True)
class CustomerDetails:
    userid: str = field(repr=False)
    session_token: str = field(repr=False)
    segments: Mapping[str, str]
    success_keys: tuple[str, ...]
    exg_trade_date_nse: str | None
    exg_status_nse: str | None
    lastlogin_raw: str | None = field(default=None, repr=False)


def parse_customer_details(success: object) -> CustomerDetails:
    """Build CustomerDetails from a normalized Success object."""
    if not isinstance(success, dict):
        raise BreezeError("UPSTREAM_SHAPE")
    userid = success.get("idirect_userid")
    token = success.get("session_token")
    if not isinstance(userid, str) or not userid:
        raise BreezeError("UPSTREAM_SHAPE")
    if not isinstance(token, str) or not token:
        raise BreezeError("UPSTREAM_SHAPE")
    seg_raw = success.get("segments_allowed")
    segments: dict[str, str] = {}
    if isinstance(seg_raw, dict):
        for key in _SEGMENT_KEYS:
            val = seg_raw.get(key)
            if isinstance(val, str) and len(val) == 1:
                segments[key] = val

    def nse(name: str) -> str | None:
        node = success.get(name)
        val = node.get("NSE") if isinstance(node, dict) else None
        return val if isinstance(val, str) else None

    last = success.get("idirect_lastlogin_time")
    return CustomerDetails(
        userid=userid,
        session_token=token,
        segments=segments,
        success_keys=tuple(sorted(str(k) for k in success)),
        exg_trade_date_nse=nse("exg_trade_date"),
        exg_status_nse=nse("exg_status"),
        lastlogin_raw=last if isinstance(last, str) else None,
    )


# ----------------------------------------------------------------- client


class BreezeClient:
    def __init__(
        self, transport: Transport, *, clock: Clock, timeouts: Timeouts = Timeouts()
    ) -> None:
        self._transport = transport
        self.clock = clock
        self._timeouts = timeouts
        self.calls = 0

    # One place where every request leaves the process.
    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeouts: Timeouts | None = None,
        max_bytes: int = JSON_MAX_BYTES,
    ) -> RawUpstream:
        self.calls += 1
        try:
            resp: HttpResponse = self._transport.request(
                method,
                url,
                headers=headers,
                body=body,
                timeouts=timeouts or self._timeouts,
                max_bytes=max_bytes,
            )
        except TransportError as exc:
            code = "UPSTREAM_TIMEOUT" if exc.kind == "timeout" else "UPSTREAM_UNREACHABLE"
            raise BreezeError(code) from None
        return RawUpstream(resp.status, resp.body, self.clock.now_utc())

    def customer_details_raw(self, *, app_key: str, api_session: str) -> RawUpstream:
        body = compact_json({"SessionToken": api_session, "AppKey": app_key})
        return self._send(
            "GET",
            V1_BASE + "customerdetails",
            headers={"Content-Type": "application/json", "User-Agent": SDK_USER_AGENT},
            body=body.encode("utf-8"),
        )

    def customer_details(self, *, app_key: str, api_session: str) -> CustomerDetails:
        raw = self.customer_details_raw(app_key=app_key, api_session=api_session)
        return parse_customer_details(normalize(raw.http_status, raw.body))

    # --- v2 historical daily bars (no checksum, empty body) ---

    def daily_bars_v2_raw(
        self,
        *,
        app_key: str,
        session_token: str,
        stock_code: str,
        exch_code: str,
        from_iso: str,
        to_iso: str,
        interval: str = V2_DAILY_INTERVAL,
    ) -> RawUpstream:
        query = urlencode(
            {
                "stock_code": stock_code,
                "exch_code": exch_code,
                "from_date": from_iso,
                "to_date": to_iso,
                "interval": interval,
                "product_type": "cash",
            },
            safe=":",
        )
        return self._send(
            "GET",
            f"{V2_HISTORICAL_URL}?{query}",
            headers={
                "Content-Type": "application/json",
                "apikey": app_key,
                "X-SessionToken": session_token,
            },
            body=None,
        )

    def daily_bars_v2(
        self,
        *,
        app_key: str,
        session_token: str,
        stock_code: str,
        exch_code: str,
        from_iso: str,
        to_iso: str,
        interval: str = V2_DAILY_INTERVAL,
    ) -> list:
        raw = self.daily_bars_v2_raw(
            app_key=app_key,
            session_token=session_token,
            stock_code=stock_code,
            exch_code=exch_code,
            from_iso=from_iso,
            to_iso=to_iso,
            interval=interval,
        )
        success = normalize(raw.http_status, raw.body)
        if not isinstance(success, list):
            raise BreezeError("UPSTREAM_SHAPE", http_status=raw.http_status)
        return success

    # --- checksummed v1 calls: the exact hashed string is the sent body ---

    def _checksummed_get(
        self,
        endpoint: str,
        payload: dict,
        *,
        app_key: str,
        secret_key: str,
        session_token: str,
    ) -> RawUpstream:
        body = compact_json(payload)
        headers = checksum_headers(
            app_key=app_key,
            secret_key=secret_key,
            session_token=session_token,
            body=body,
            now_utc=self.clock.now_utc(),
        )
        headers["User-Agent"] = SDK_USER_AGENT
        return self._send("GET", V1_BASE + endpoint, headers=headers, body=body.encode("utf-8"))

    def quote_raw(
        self,
        *,
        app_key: str,
        secret_key: str,
        session_token: str,
        stock_code: str,
        exchange_code: str,
    ) -> RawUpstream:
        return self._checksummed_get(
            "quotes",
            {
                "stock_code": stock_code,
                "exchange_code": exchange_code,
                "expiry_date": "",
                "product_type": "cash",
                "right": "",
                "strike_price": "",
            },
            app_key=app_key,
            secret_key=secret_key,
            session_token=session_token,
        )

    def quote(
        self,
        *,
        app_key: str,
        secret_key: str,
        session_token: str,
        stock_code: str,
        exchange_code: str,
    ) -> object:
        raw = self.quote_raw(
            app_key=app_key,
            secret_key=secret_key,
            session_token=session_token,
            stock_code=stock_code,
            exchange_code=exchange_code,
        )
        return normalize(raw.http_status, raw.body)

    def preview_order_raw(
        self,
        *,
        app_key: str,
        secret_key: str,
        session_token: str,
        stock_code: str,
        exchange_code: str,
        action: str,
        quantity: int,
        price: str,
    ) -> RawUpstream:
        # preview_order is a brokerage calculator, not an order. Validate first.
        if action not in ("buy", "sell"):
            raise ValueError("action must be buy or sell")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("quantity must be a positive int")
        if not isinstance(price, str) or not _PRICE_RE.match(price) or float(price) <= 0:
            raise ValueError("price must be a positive decimal string, at most 2 decimals")
        return self._checksummed_get(
            "preview_order",
            {
                "stock_code": stock_code,
                "exchange_code": exchange_code,
                "product": "cash",
                "order_type": "limit",
                "price": price,
                "action": action,
                "quantity": str(quantity),
                "specialflag": "N",
            },
            app_key=app_key,
            secret_key=secret_key,
            session_token=session_token,
        )

    def preview_order(
        self,
        *,
        app_key: str,
        secret_key: str,
        session_token: str,
        stock_code: str,
        exchange_code: str,
        action: str,
        quantity: int,
        price: str,
    ) -> object:
        raw = self.preview_order_raw(
            app_key=app_key,
            secret_key=secret_key,
            session_token=session_token,
            stock_code=stock_code,
            exchange_code=exchange_code,
            action=action,
            quantity=quantity,
            price=price,
        )
        return normalize(raw.http_status, raw.body)

    # --- security master: a zip, not JSON, so it never goes through normalize ---

    def security_master_raw(self, url: str, *, timeouts: Timeouts, max_bytes: int) -> RawUpstream:
        return self._send(
            "GET",
            url,
            headers={"User-Agent": SDK_USER_AGENT},
            body=None,
            timeouts=timeouts,
            max_bytes=max_bytes,
        )
