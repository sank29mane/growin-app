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
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote_plus

from .breeze_client import (
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


def validate_result(obj: object) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        return ["result is not an object"]
    if obj.get("schema") != RESULT_SCHEMA:
        problems.append("schema mismatch")
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
        # S2 to S5 are added in the next task.

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
