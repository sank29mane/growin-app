"""Standalone read-only Breeze smoke probe (S0 to S5), run once on the VM.

The probe answers the unknowns later plans must read instead of assume. It has
no order code path. Secrets come from hidden prompts and are never stored. It
runs the egress check first and sends no broker request unless the VM's egress
IP is the registered one (D-13, GATE-02).

The result file holds booleans, counts, formats and public market data only.
Before writing, every secret value is searched for in the serialized result in
raw, URL-encoded and base64 form; a hit writes nothing and exits 4.

Exit codes: 0 ok, 2 no TTY or bad args, 3 egress failed, 4 redaction
self-check failed, 5 S1 failed.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hmac
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import reduce
from math import gcd
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote_plus

from .breeze_client import (
    V2_DAILY_INTERVAL,
    BreezeClient,
    BreezeError,
    classify_payload,
    normalize,
    parse_customer_details,
)
from .egress import EgressChecker
from .transport import HttpClientTransport, SystemClock

RESULT_SCHEMA = "growin.smoke.breeze.v1"
RESULT_FILENAME = "61-smoke-breeze.json"
PROBE_VERSION = "61-01"
DEFAULT_OUT = "/dev/shm/growin-probe/out"
MAX_CALLS = 20
ALL_STEPS = ("S0", "S1", "S2", "S3", "S4", "S5")
ECHO_URL = "https://api.ipify.org"

INSTRUCTION = (
    "Open the ICICI API portal in a private window, View Apps, Login, complete "
    "the OTP, then copy the apisession value from the address bar of the page "
    "that fails to load at https://127.0.0.1/ and paste it at the next prompt."
)

_IST = timezone(timedelta(hours=5, minutes=30))
_LASTLOGIN_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S")
_ALLOWED_SEGMENTS = ("Trading", "Equity", "Derivatives", "Currency")


class RedactionError(Exception):
    """A secret value was found in the serialized result; nothing was written."""


@dataclass(frozen=True)
class Prompts:
    app_key: Callable[[], str]
    secret_key: Callable[[], str]
    expected_userid: Callable[[], str]
    expected_ip: Callable[[], str]
    api_session: Callable[[], str]


def default_prompts() -> Prompts:
    """Hidden prompts for every value, including the expected IP and user id."""
    return Prompts(
        app_key=lambda: getpass.getpass("AppKey (hidden): "),
        secret_key=lambda: getpass.getpass("Secret key (hidden): "),
        expected_userid=lambda: getpass.getpass("Expected ICICI user id (hidden): "),
        expected_ip=lambda: getpass.getpass("Expected egress IP (hidden): "),
        api_session=lambda: getpass.getpass("API_Session (hidden): "),
    )


@dataclass(frozen=True)
class Window:
    stock_code: str
    from_date: str
    to_date: str
    ex_date: str | None = None


@dataclass(frozen=True)
class ProbePlan:
    bajfi: Window = Window("BAJFI", "2025-06-09", "2025-06-20", "2025-06-16")
    relind: Window = Window("RELIND", "2024-10-21", "2024-10-31", "2024-10-28")
    depth: Window = Window("RELIND", "2021-10-01", "2026-09-30", None)
    quote_codes: tuple[str, ...] = ("RELIND", "BAJFI", "NIFBEE")
    preview_code: str = "RELIND"
    notional: int = 50000


# --------------------------------------------------------------- redaction


def _forms(secret: str) -> set[str]:
    raw = secret.encode("utf-8")
    forms = {
        secret,
        quote_plus(secret),
        json.dumps(secret)[1:-1],
        base64.b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii"),
    }
    forms |= {f.rstrip("=") for f in list(forms)}
    return {f for f in forms if len(f) >= 4}


def assert_redacted(serialized: str, secrets_found: Sequence[str]) -> None:
    for secret in secrets_found:
        if not secret:
            continue
        for form in _forms(secret):
            if form in serialized:
                raise RedactionError("secret value present in result")


# ------------------------------------------------------------------ state


@dataclass
class _State:
    client: BreezeClient
    sleep: Callable[[float], None]
    plan: ProbePlan
    app_key: str
    secret_key: str
    expected_userid: str
    api_session: str = ""
    token: str = ""
    secrets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    made_call: bool = False

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def may_call(self) -> bool:
        """Enforce the hard call cap and the 1 s pacing between Breeze calls."""
        if self.client.calls >= MAX_CALLS:
            self.note("call_cap_reached")
            return False
        if self.made_call:
            self.sleep(1.0)
        self.made_call = True
        return True


# ------------------------------------------------------------------- steps


def _status_info(body: bytes) -> tuple[int | str | None, str]:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return None, "other"
    if not isinstance(payload, dict):
        return None, "other"
    status = payload.get("Status")
    if isinstance(status, bool):
        return None, "other"
    if isinstance(status, int):
        return status, "int"
    if isinstance(status, str):
        return status[:20], "str"
    if status is None:
        return None, "null"
    return None, "other"


def _charset(code: str) -> str:
    if code.isdigit():
        return "digits"
    if code.isalpha():
        return "letters"
    if code.isalnum():
        return "alnum"
    return "other"


def _token_is_b64_pair(token: str) -> bool:
    try:
        text = base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
    except Exception:
        return False
    if text.count(":") != 1:
        return False
    left, right = text.split(":")
    return bool(left and right)


def _lastlogin(raw: str | None, now_utc: datetime) -> tuple[str, str]:
    if not raw:
        return "unparsed", "unknown"
    for fmt in _LASTLOGIN_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        window = timedelta(minutes=15)
        ist = now_utc.astimezone(_IST).replace(tzinfo=None)
        utc = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
        if abs(parsed - ist) <= window:
            return fmt, "IST"
        if abs(parsed - utc) <= window:
            return fmt, "UTC"
        return fmt, "unknown"
    return "unparsed", "unknown"


def _s1_defaults(api_session: str) -> dict:
    return {
        "pass": False,
        "http": 0,
        "status_value": None,
        "status_type": "other",
        "error_class": None,
        "success_keys": [],
        "userid_matches": False,
        "segments": {},
        "code_length": len(api_session),
        "code_charset": _charset(api_session),
        "token_b64_pair": False,
        "token_length": 0,
        "exg_trade_date_nse": None,
        "exg_status_nse": None,
        "lastlogin_format": "unparsed",
        "lastlogin_tz_hint": "unknown",
    }


def _step_s1(st: _State) -> dict:
    out = _s1_defaults(st.api_session)
    if not st.may_call():
        out["error_class"] = "other"
        return out
    try:
        raw = st.client.customer_details_raw(app_key=st.app_key, api_session=st.api_session)
    except BreezeError:
        out["error_class"] = "other"
        return out
    out["http"] = raw.http_status
    out["status_value"], out["status_type"] = _status_info(raw.body)
    verdict = classify_payload(raw.http_status, raw.body)
    out["error_class"] = verdict.error_class
    if not verdict.ok:
        return out
    try:
        details = parse_customer_details(normalize(raw.http_status, raw.body))
    except BreezeError:
        out["error_class"] = "other"
        return out
    st.secrets.append(details.session_token)
    st.token = details.session_token
    out["success_keys"] = list(details.success_keys)
    out["userid_matches"] = hmac.compare_digest(
        details.userid.encode("utf-8"), st.expected_userid.encode("utf-8")
    )
    out["segments"] = {k: v for k, v in details.segments.items() if k in _ALLOWED_SEGMENTS}
    out["token_b64_pair"] = _token_is_b64_pair(details.session_token)
    out["token_length"] = len(details.session_token)
    out["exg_trade_date_nse"] = details.exg_trade_date_nse
    out["exg_status_nse"] = details.exg_status_nse
    out["lastlogin_format"], out["lastlogin_tz_hint"] = _lastlogin(
        details.lastlogin_raw, raw.fetched_at_utc
    )
    out["pass"] = bool(out["userid_matches"])
    return out


# --------------------------------------------------------------- S2 to S5

# NSE price-based tick table (from 15-Apr-2025): (exclusive upper bound, tick in paise).
NSE_TICK_TABLE = ((250, 1), (1000, 5), (5000, 10), (10000, 50), (20000, 100))
NSE_TOP_TICK_PAISE = 500

_DT_FORMATS = (
    (re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "YYYY-MM-DD HH:MM:SS"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"), "YYYY-MM-DDTHH:MM:SS"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "YYYY-MM-DD"),
)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2})")


def nse_table_tick_paise(price: float | None) -> int | None:
    if price is None or price <= 0:
        return None
    for bound, tick in NSE_TICK_TABLE:
        if price < bound:
            return tick
    return NSE_TOP_TICK_PAISE


def _num(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _json(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def _first_dict(success: object) -> dict | None:
    if isinstance(success, dict):
        return success
    if isinstance(success, list):
        for entry in success:
            if isinstance(entry, dict):
                return entry
    return None


def _envelope(item: dict, raw_body: bytes, http: int) -> object:
    """Fill the fields every raw call records. Returns the parsed payload."""
    item["http"] = http
    item["status_value"], _type = _status_info(raw_body)
    payload = _json(raw_body)
    verdict = classify_payload(http, raw_body)
    item["error_class"] = verdict.error_class
    return payload


def _error_text(payload: object) -> str | None:
    if isinstance(payload, dict):
        err = payload.get("Error")
        if isinstance(err, str) and err:
            return err[:200]
    return None


def _ratio(after: float | None, before: float | None) -> float | None:
    if after is None or before in (None, 0):
        return None
    return round(after / before, 4)


def _bars_item(
    st: _State, item_id: str, window: Window, interval: str, expected_ratio: float | None
) -> dict:
    upstream_from = f"{window.from_date}T00:00:00.000Z"
    upstream_to = f"{window.to_date}T23:59:59.000Z"
    item: dict = {
        "id": item_id,
        "stock_code": window.stock_code,
        "from": window.from_date,
        "to": window.to_date,
        "ex_date": window.ex_date,
        "interval": interval,
        "upstream_from": upstream_from,
        "upstream_to": upstream_to,
        "http": 0,
        "status_value": None,
        "envelope_keys": [],
        "error_class": None,
        "error_text": None,
        "rows": 0,
        "row_keys": [],
        "value_types": {},
        "datetime_format": "other",
        "time_of_day": [],
        "first_date": None,
        "last_date": None,
        "boundary_first_included": False,
        "boundary_last_included": False,
        "pre_ex_close": None,
        "ex_close": None,
        "close_ratio": None,
        "volume_ratio": None,
        "adjustment_reading": "n/a",
        "capped_at_1000": False,
    }
    if not st.may_call():
        item["error_class"] = "other"
        item["error_text"] = "call_cap_reached"
        return item
    try:
        raw = st.client.daily_bars_v2_raw(
            app_key=st.app_key,
            session_token=st.token,
            stock_code=window.stock_code,
            exch_code="NSE",
            from_iso=upstream_from,
            to_iso=upstream_to,
            interval=interval,
        )
    except BreezeError as exc:
        item["error_class"] = "other"
        item["error_text"] = str(exc)
        return item

    payload = _envelope(item, raw.body, raw.http_status)
    if isinstance(payload, dict):
        item["envelope_keys"] = sorted(str(k) for k in payload)
    item["error_text"] = _error_text(payload)
    verdict = classify_payload(raw.http_status, raw.body)
    rows = payload.get("Success") if verdict.ok and isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    dicts = [r for r in rows if isinstance(r, dict)]
    item["rows"] = len(rows)
    item["capped_at_1000"] = len(rows) == 1000
    item["row_keys"] = sorted({str(k) for r in dicts for k in r})
    types: dict[str, set[str]] = {}
    for r in dicts:
        for key, value in r.items():
            types.setdefault(str(key), set()).add(type(value).__name__)
    item["value_types"] = {k: "|".join(sorted(v)) for k, v in sorted(types.items())}

    stamps = [str(r.get("datetime", "")) for r in dicts]
    formats = set()
    for stamp in stamps:
        formats.add(next((name for rx, name in _DT_FORMATS if rx.match(stamp)), "other"))
    if len(formats) == 1:
        item["datetime_format"] = next(iter(formats))
    dates = [m.group(1) for s in stamps if (m := _DATE_RE.match(s))]
    item["time_of_day"] = sorted({m.group(1) for s in stamps if (m := _TIME_RE.search(s))})[:5]
    if dates:
        item["first_date"], item["last_date"] = min(dates), max(dates)
        item["boundary_first_included"] = item["first_date"] == window.from_date
        item["boundary_last_included"] = item["last_date"] == window.to_date

    if window.ex_date and expected_ratio is not None and dates:
        by_date: dict[str, dict] = {}
        for r in dicts:
            m = _DATE_RE.match(str(r.get("datetime", "")))
            if m:
                by_date.setdefault(m.group(1), r)
        before = [d for d in sorted(by_date) if d < window.ex_date]
        after = [d for d in sorted(by_date) if d >= window.ex_date]
        if before and after:
            pre, ex = by_date[before[-1]], by_date[after[0]]
            item["pre_ex_close"] = _num(pre.get("close"))
            item["ex_close"] = _num(ex.get("close"))
            ratio = _ratio(item["ex_close"], item["pre_ex_close"])
            item["close_ratio"] = ratio
            item["volume_ratio"] = _ratio(_num(ex.get("volume")), _num(pre.get("volume")))
            if ratio is None:
                item["adjustment_reading"] = "inconclusive"
            elif abs(ratio - expected_ratio) <= 0.25 * expected_ratio:
                item["adjustment_reading"] = "raw"
            elif abs(ratio - 1) <= 0.1:
                item["adjustment_reading"] = "adjusted"
            else:
                item["adjustment_reading"] = "inconclusive"
        else:
            item["adjustment_reading"] = "inconclusive"
    return item


def _step_s2(st: _State, result: dict) -> None:
    plan = st.plan
    items = [
        _bars_item(st, "S2a", plan.bajfi, V2_DAILY_INTERVAL, 0.10),
        _bars_item(st, "S2b", plan.relind, V2_DAILY_INTERVAL, 0.5),
        _bars_item(st, "S2c", plan.depth, V2_DAILY_INTERVAL, None),
    ]
    wire = "1day" if any(i["rows"] > 0 for i in items) else None
    if wire is None:
        retry = _bars_item(st, "S2d", plan.bajfi, "day", 0.10)
        items.append(retry)
        wire = "day" if retry["rows"] > 0 else "none"
    result["s2_bars"] = items
    result["s2_interval_wire"] = wire


def tick_gcd_paise(values: Sequence[float | None]) -> int | None:
    ints: list[int] = []
    for value in values:
        if value is None or value <= 0:
            continue
        paise = Decimal(str(value)) * 100
        if paise != paise.to_integral_value():
            continue
        ints.append(int(paise))
    return reduce(gcd, ints) if ints else None


def _quote_item(st: _State, code: str) -> dict:
    item: dict = {
        "stock_code": code,
        "http": 0,
        "status_value": None,
        "error_class": None,
        "keys": [],
        "has_circuits": False,
        "ltp": None,
        "best_bid_price": None,
        "best_offer_price": None,
        "upper_circuit": None,
        "lower_circuit": None,
        "previous_close": None,
        "ltt": None,
        "inferred_tick_paise": None,
        "nse_table_tick_paise": None,
    }
    if not st.may_call():
        item["error_class"] = "other"
        return item
    try:
        raw = st.client.quote_raw(
            app_key=st.app_key,
            secret_key=st.secret_key,
            session_token=st.token,
            stock_code=code,
            exchange_code="NSE",
        )
    except BreezeError:
        item["error_class"] = "other"
        return item
    payload = _envelope(item, raw.body, raw.http_status)
    if not item["error_class"] and isinstance(payload, dict):
        fields = _first_dict(payload.get("Success"))
        if fields is not None:
            item["keys"] = sorted(str(k) for k in fields)
            item["has_circuits"] = "upper_circuit" in fields and "lower_circuit" in fields
            for key in (
                "ltp", "best_bid_price", "best_offer_price", "upper_circuit",
                "lower_circuit", "previous_close",
            ):
                item[key] = _num(fields.get(key))
            ltt = fields.get("ltt")
            item["ltt"] = ltt[:40] if isinstance(ltt, str) else None
            item["inferred_tick_paise"] = tick_gcd_paise(
                [item[k] for k in (
                    "ltp", "best_bid_price", "best_offer_price", "upper_circuit", "lower_circuit",
                )]
            )
            item["nse_table_tick_paise"] = nse_table_tick_paise(item["ltp"])
    return item


def _step_s3(st: _State, result: dict) -> None:
    result["s3_quotes"] = [_quote_item(st, code) for code in st.plan.quote_codes]


def _preview_item(st: _State, action: str, quantity: int, price: str) -> dict:
    item: dict = {
        "stock_code": st.plan.preview_code,
        "action": action,
        "quantity": quantity,
        "price": price,
        "http": 0,
        "status_value": None,
        "error_class": None,
        "error_text": None,
        "lines": {},
    }
    if not st.may_call():
        item["error_class"] = "other"
        return item
    try:
        raw = st.client.preview_order_raw(
            app_key=st.app_key,
            secret_key=st.secret_key,
            session_token=st.token,
            stock_code=st.plan.preview_code,
            exchange_code="NSE",
            action=action,
            quantity=quantity,
            price=price,
        )
    except BreezeError:
        item["error_class"] = "other"
        return item
    payload = _envelope(item, raw.body, raw.http_status)
    item["error_text"] = _error_text(payload)
    if not item["error_class"] and isinstance(payload, dict):
        fields = _first_dict(payload.get("Success"))
        if fields is not None:
            item["lines"] = {
                str(k): n for k, v in fields.items() if (n := _num(v)) is not None
            }
    return item


def _step_s4(st: _State, result: dict) -> None:
    result["s4_preview"] = []
    base = next(
        (q for q in result.get("s3_quotes", []) if q["stock_code"] == st.plan.preview_code), None
    )
    ltp = base["ltp"] if base else None
    if not ltp or ltp <= 0:
        st.note("s4_skipped_no_ltp")
        return
    tick = (base["inferred_tick_paise"] or 5)
    paise = int(Decimal(str(ltp)) * 100) // tick * tick
    if paise <= 0:
        st.note("s4_skipped_no_ltp")
        return
    price = f"{Decimal(paise) / 100:.2f}"
    big = max(1, int(Decimal(st.plan.notional) // Decimal(price)))
    for action in ("buy", "sell"):
        for quantity in (1, big):
            result["s4_preview"].append(_preview_item(st, action, quantity, price))


def _step_s5(st: _State, result: dict) -> None:
    out: dict = {
        "second_exchange": "error",
        "error_class": None,
        "tokens_equal": None,
        "first_token_still_works": False,
    }
    result["s5_replay"] = out
    if st.may_call():
        try:
            raw = st.client.customer_details_raw(app_key=st.app_key, api_session=st.api_session)
        except BreezeError:
            out["error_class"] = "other"
        else:
            verdict = classify_payload(raw.http_status, raw.body)
            if verdict.ok:
                try:
                    details = parse_customer_details(normalize(raw.http_status, raw.body))
                except BreezeError:
                    out["error_class"] = "other"
                else:
                    st.secrets.append(details.session_token)
                    out["second_exchange"] = "success"
                    out["tokens_equal"] = hmac.compare_digest(
                        details.session_token.encode("utf-8"), st.token.encode("utf-8")
                    )
            else:
                out["error_class"] = verdict.error_class
    # The last Breeze call: does the first token still work?
    if st.may_call():
        try:
            raw = st.client.quote_raw(
                app_key=st.app_key,
                secret_key=st.secret_key,
                session_token=st.token,
                stock_code=st.plan.preview_code,
                exchange_code="NSE",
            )
        except BreezeError:
            pass
        else:
            out["first_token_still_works"] = classify_payload(raw.http_status, raw.body).ok


# ----------------------------------------------------------------- writer


def write_result(out_dir: Path, result: dict) -> Path:
    """Write the one result file: 0600 in a 0700 directory, never overwritten."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        if out_dir.stat().st_mode & 0o077:
            raise PermissionError("output directory must not be group or world accessible")
    else:
        os.makedirs(out_dir, mode=0o700, exist_ok=True)
        os.chmod(out_dir, 0o700)
    path = out_dir / RESULT_FILENAME
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return path


# -------------------------------------------------------------- validation


_TOP_KEYS = (
    "schema", "probe_version", "run_at_utc", "python", "calls", "s0_egress",
    "s1_customer_details", "s2_bars", "s2_interval_wire", "s3_quotes", "s4_preview",
    "s5_replay", "notes",
)
_S0_KEYS = ("ok", "metadata_match", "echo_match")
_S1_KEYS = (
    "pass", "http", "status_value", "status_type", "error_class", "success_keys",
    "userid_matches", "segments", "code_length", "code_charset", "token_b64_pair",
    "token_length", "exg_trade_date_nse", "exg_status_nse", "lastlogin_format",
    "lastlogin_tz_hint",
)
_S2_KEYS = (
    "id", "stock_code", "from", "to", "ex_date", "interval", "upstream_from", "upstream_to",
    "http", "status_value", "envelope_keys", "error_class", "error_text", "rows", "row_keys",
    "value_types", "datetime_format", "time_of_day", "first_date", "last_date",
    "boundary_first_included", "boundary_last_included", "pre_ex_close", "ex_close",
    "close_ratio", "volume_ratio", "adjustment_reading", "capped_at_1000",
)
_S3_KEYS = (
    "stock_code", "http", "status_value", "error_class", "keys", "has_circuits", "ltp",
    "best_bid_price", "best_offer_price", "upper_circuit", "lower_circuit", "previous_close",
    "ltt", "inferred_tick_paise", "nse_table_tick_paise",
)
_S4_KEYS = (
    "stock_code", "action", "quantity", "price", "http", "status_value", "error_class",
    "error_text", "lines",
)
_S5_KEYS = ("second_exchange", "error_class", "tokens_equal", "first_token_still_works")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_B64_SHAPE = re.compile(r"^[A-Za-z0-9+/]{8,}={0,2}$")


def _looks_like_secret(value: str) -> bool:
    text = value.strip()
    if _HEX64.match(text):
        return True
    if _B64_SHAPE.match(text):
        try:
            decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True).decode("ascii")
        except Exception:
            return False
        left, sep, right = decoded.partition(":")
        return bool(sep and left and right and ":" not in right)
    return False


def _walk_strings(node: object, path: str, problems: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_strings(value, f"{path}.{key}" if path else str(key), problems)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_strings(value, f"{path}[{index}]", problems)
    elif isinstance(node, str) and _looks_like_secret(node):
        problems.append(f"secret-shaped string at {path}")


def _check_keys(node: object, expected: Sequence[str], where: str, problems: list[str]) -> None:
    if not isinstance(node, dict):
        problems.append(f"{where} is not an object")
        return
    for key in expected:
        if key not in node:
            problems.append(f"{where} missing key {key}")
    for key in node:
        if key not in expected:
            problems.append(f"{where} has unexpected key {key}")


def validate_result(obj: object) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        return ["result is not an object"]
    _check_keys(obj, _TOP_KEYS, "result", problems)
    if obj.get("schema") != RESULT_SCHEMA:
        problems.append("schema mismatch")
    calls = obj.get("calls")
    if not (
        isinstance(calls, dict)
        and isinstance(calls.get("breeze"), int)
        and isinstance(calls.get("max"), int)
        and calls["breeze"] <= calls["max"] <= MAX_CALLS
    ):
        problems.append("calls out of bounds")
    if obj.get("s2_interval_wire") not in ("1day", "day", "none"):
        problems.append("s2_interval_wire invalid")
    if "s0_egress" in obj:
        _check_keys(obj["s0_egress"], _S0_KEYS, "s0_egress", problems)
    if "s1_customer_details" in obj:
        _check_keys(obj["s1_customer_details"], _S1_KEYS, "s1_customer_details", problems)
    for key, expected in (("s2_bars", _S2_KEYS), ("s3_quotes", _S3_KEYS), ("s4_preview", _S4_KEYS)):
        items = obj.get(key)
        if not isinstance(items, list):
            if key in obj:
                problems.append(f"{key} is not a list")
            continue
        for index, item in enumerate(items):
            _check_keys(item, expected, f"{key}[{index}]", problems)
    if "s5_replay" in obj:
        _check_keys(obj["s5_replay"], _S5_KEYS, "s5_replay", problems)
    if not isinstance(obj.get("notes", []), list):
        problems.append("notes is not a list")
    _walk_strings(obj, "", problems)
    return problems


# ------------------------------------------------------------------- probe


def run_probe(
    *,
    client: BreezeClient,
    egress_factory: Callable[[str], EgressChecker],
    prompts: Prompts,
    plan: ProbePlan,
    out_dir: Path,
    sleep: Callable[[float], None],
    steps: Sequence[str] = ALL_STEPS,
) -> dict:
    """Run S0 (always) then the requested steps, write the redacted result."""
    app_key = prompts.app_key()
    secret_key = prompts.secret_key()
    expected_userid = prompts.expected_userid()
    expected_ip = prompts.expected_ip()
    st = _State(
        client=client,
        sleep=sleep,
        plan=plan,
        app_key=app_key,
        secret_key=secret_key,
        expected_userid=expected_userid,
    )
    st.secrets.extend([app_key, secret_key, expected_userid, expected_ip])

    result: dict = {
        "schema": RESULT_SCHEMA,
        "probe_version": PROBE_VERSION,
        "run_at_utc": client.clock.now_utc().astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "python": platform.python_version(),
        "calls": {"breeze": 0, "max": MAX_CALLS},
    }

    # S0 always runs first: no broker request leaves unless egress is right.
    egress = egress_factory(expected_ip).check()
    result["s0_egress"] = {
        "ok": egress.ok,
        "metadata_match": egress.metadata_match,
        "echo_match": egress.echo_match,
    }
    if egress.ok:
        print(INSTRUCTION)
        st.api_session = prompts.api_session()
        st.secrets.append(st.api_session)
        if "S1" in steps:
            result["s1_customer_details"] = _step_s1(st)
            s1_ok = result["s1_customer_details"]["pass"]
            # An S1 failure stops the run; any other failing step records and continues.
            runners = (
                ("S2", _step_s2),
                ("S3", _step_s3),
                ("S4", _step_s4),
                ("S5", _step_s5),
            )
            for name, runner in runners:
                if not s1_ok or name not in steps:
                    continue
                try:
                    runner(st, result)
                except Exception:
                    st.note(f"{name.lower()}_exception")

    result["calls"]["breeze"] = client.calls
    result["notes"] = list(st.notes)
    assert_redacted(json.dumps(result), st.secrets)
    write_result(out_dir, result)
    return result


# -------------------------------------------------------------------- main


def _parse_window(text: str, stock_code: str) -> Window:
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("expected FROM:TO[:EX]")
    for part in parts:
        try:
            datetime.strptime(part, "%Y-%m-%d")
        except ValueError:
            raise argparse.ArgumentTypeError("dates must be YYYY-MM-DD") from None
    return Window(stock_code, parts[0], parts[1], parts[2] if len(parts) == 3 else None)


def _build_runtime():
    transport = HttpClientTransport()
    clock = SystemClock()
    client = BreezeClient(transport, clock=clock)

    def egress_factory(expected_ip: str) -> EgressChecker:
        return EgressChecker(transport, expected_ip=expected_ip, echo_url=ECHO_URL, clock=clock)

    return client, egress_factory, time.sleep


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smoke_probe", description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--validate", metavar="FILE", help="validate a result file and exit")
    parser.add_argument("--bajfi-window", metavar="FROM:TO[:EX]")
    parser.add_argument("--relind-window", metavar="FROM:TO[:EX]")
    parser.add_argument("--depth-window", metavar="FROM:TO")
    return parser


def _plan_from_args(args: argparse.Namespace) -> ProbePlan:
    base = ProbePlan()
    return ProbePlan(
        bajfi=_parse_window(args.bajfi_window, "BAJFI") if args.bajfi_window else base.bajfi,
        relind=_parse_window(args.relind_window, "RELIND") if args.relind_window else base.relind,
        depth=_parse_window(args.depth_window, "RELIND") if args.depth_window else base.depth,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        plan = _plan_from_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except argparse.ArgumentTypeError as exc:
        print(f"bad argument: {exc}")
        return 2

    if args.validate:
        try:
            obj = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("cannot read result file")
            return 1
        problems = validate_result(obj)
        if problems:
            for problem in problems:
                print(f"PROBLEM {problem}")
            return 1
        print("OK")
        return 0

    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        print("a TTY is required: secrets are read from hidden prompts")
        return 2

    client, egress_factory, sleep = _build_runtime()
    prompts = default_prompts()
    try:
        result = run_probe(
            client=client,
            egress_factory=egress_factory,
            prompts=prompts,
            plan=plan,
            out_dir=Path(args.out),
            sleep=sleep,
        )
    except RedactionError:
        print("redaction self-check failed: nothing written")
        return 4
    except (FileExistsError, PermissionError) as exc:
        print(f"cannot write result: {type(exc).__name__}")
        return 2
    finally:
        del prompts

    # Allowlisted summary only: booleans, counts and formats.
    print(f"S0 egress ok={result['s0_egress']['ok']}")
    if "s1_customer_details" in result:
        s1 = result["s1_customer_details"]
        print(f"S1 pass={s1['pass']} error_class={s1['error_class']}")
    print(f"breeze calls={result['calls']['breeze']}")
    if not result["s0_egress"]["ok"]:
        return 3
    if not result["s1_customer_details"]["pass"]:
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
