#!/usr/bin/env python3
"""Operator-run Trading 212 practice smoke driver (Phase 66, D-06, D-27).

This is the 66-05 tool. It drives the practice smoke one confirmed step at a time
through the LOOPBACK backend and nothing else:

* It never signs. Every order is approved by Touch ID in the app; the CLI only
  prepares, waits, and reconciles.
* It never calls Trading 212 and never any non-loopback URL: its HTTP client
  refuses any host but 127.0.0.1, localhost or ::1 before a request is built.
* It reads no environment variable and holds no key. The evidence it writes keeps
  only the last four characters of the account id.
* Every step needs a typed confirmation that names the ticker (and side). Any
  other input aborts the run with nothing sent for that step.

Step order (D-27): ``buy`` x3 (far from the market, three different tickers),
``cancel`` (one of those three), ``buy-q1`` (one whole marketable share on a
fourth ticker), ``sell`` (that share), then, on a later day, the separate
``reconcile-all`` command for the next-day DAY expiry of the remaining far buys.

Usage:
    uv run --no-sync --project backend python scripts/t212_practice_smoke.py
    uv run --no-sync --project backend python scripts/t212_practice_smoke.py reconcile-all

Run only by the operator, in the 66-05 environment. Never in CI, never by an agent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

DEFAULT_BACKEND = "http://127.0.0.1:8000"
DEFAULT_EVIDENCE = Path("66-SMOKE-PRACTICE")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
FAR_FACTOR = Decimal("0.8")  # a far buy limit is at most 80% of the recorded bid (D-06)
READINGS_PER_ORDER = 3
TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED", "FAILED", "UNKNOWN"})
NOT_SENT_STATES = frozenset({"PENDING"})
# Reconcile codes that mean the evidence is consistent (or simply not there yet).
# Anything else is an anomaly and stops the run: fail closed on unknown codes.
HEALTHY_RECONCILE_CODES = frozenset(
    {"ADOPTED", "APPLIED", "UNCHANGED", "NOT_VISIBLE", "FILL_EVIDENCE_PENDING", "NO_MATCH", "NOT_RECONCILABLE"}
)
HEALTHY_POSITION_CHECKS = frozenset({"OK", "SKIPPED"})
STEP_ORDER = ("buy-1", "buy-2", "buy-3", "cancel", "buy-q1", "sell")

PREPARE = "/api/t212-practice/preparations"
PROPOSALS = "/api/t212-practice/proposals"
RECONCILE = "/api/t212-practice/reconciliations"
CANCEL = "/api/t212-practice/cancellations"
STATUS = "/api/system/status"


class SmokeError(Exception):
    """A step cannot go on. Nothing further is sent."""


class SmokeAbort(SmokeError):
    """The operator typed something other than the confirmation."""


# --- a loopback-only HTTP client ------------------------------------------------------------


class LoopbackClient:
    """JSON over HTTP to the local backend. Any other host is refused before sending."""

    def __init__(self, base_url: str = DEFAULT_BACKEND, *, transport=None, timeout: float = 150.0):
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in LOOPBACK_HOSTS or "trading212" in base_url.lower():
            raise SmokeError("the smoke talks only to the loopback backend")
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        import httpx

        self._http = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=timeout, follow_redirects=False
        )
        self._hook_installed = False

    def _checked(self, path: str) -> str:
        if not path.startswith("/api/") or "://" in path or path.startswith("//"):
            raise SmokeError("only backend API paths are allowed")
        return path

    def get(self, path: str) -> tuple[int, Any]:
        return self._send("GET", path, None)

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        return self._send("POST", path, payload)

    def _send(self, method: str, path: str, payload: Optional[dict[str, Any]]) -> tuple[int, Any]:
        response = self._http.request(method, self._checked(path), json=payload)
        if response.is_redirect:
            raise SmokeError("a redirect from the backend was not followed")
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:200]}
        return response.status_code, body


# --- evidence (redacted) -----------------------------------------------------------------------------


@dataclass
class Evidence:
    path: Path
    account_last4: str = "????"
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def json_path(self) -> Path:
        return self.path.with_suffix(".json")

    @property
    def md_path(self) -> Path:
        return self.path.with_suffix(".md")

    def load(self) -> None:
        if self.json_path.exists():
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            self.account_last4 = data.get("account", self.account_last4)
            self.steps = list(data.get("steps", []))

    def add(self, step: dict[str, Any]) -> dict[str, Any]:
        self.steps.append(step)
        self.save()
        return step

    def save(self) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"account": self.account_last4, "steps": self.steps}
        self.json_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        lines = [
            "# Phase 66 practice smoke evidence (redacted)",
            "",
            f"Account: {self.account_last4} (last four characters only). No key material is recorded.",
            "",
        ]
        for index, step in enumerate(self.steps, 1):
            lines.append(f"## {index}. {step.get('step')} - {step.get('ticker', '')}")
            for key in (
                "side", "quantity", "limit_price", "confirmation", "proposal_id", "admission",
                "reason_code", "touch_id", "states", "result", "note", "anomaly",
            ):
                if key in step:
                    lines.append(f"- {key}: {step[key]}")
            for reading in step.get("readings", []):
                lines.append(f"- reading {reading['at']}: bid {reading['bid']} ask {reading['ask']}")
            lines.append("")
        self.md_path.write_text("\n".join(lines), encoding="utf-8")

    def step_for(self, name: str) -> Optional[dict[str, Any]]:
        for step in reversed(self.steps):
            if step.get("step") == name:
                return step
        return None


def last_four(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "")
    return "****" + cleaned[-4:] if cleaned else "????"


# --- the driver --------------------------------------------------------------------------------------------


class Smoke:
    def __init__(
        self,
        client: Any,
        ask: Callable[[str], str],
        say: Callable[[str], None],
        evidence: Evidence,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 3.0,
        approval_polls: int = 100,
        reconcile_polls: int = 12,
        reconcile_seconds: float = 10.0,
    ) -> None:
        self.client = client
        self.ask = ask
        self.say = say
        self.evidence = evidence
        self.clock = clock
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.approval_polls = approval_polls
        self.reconcile_polls = reconcile_polls
        self.reconcile_seconds = reconcile_seconds
        self.far_tickers: list[str] = []

    # -- plumbing ---------------------------------------------------------------------------------

    def _call(self, method: str, path: str, payload: Optional[dict[str, Any]] = None):
        status, body = (
            self.client.get(path) if method == "GET" else self.client.post(path, payload or {})
        )
        if status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else str(detail)[:60]
            raise SmokeError(f"the backend refused the request ({status} {code})")
        return body

    def confirm(self, phrase: str) -> str:
        typed = self.ask(f'Type exactly "{phrase}" to send this step (anything else aborts): ').strip()
        if typed != phrase:
            raise SmokeAbort("not confirmed: nothing was sent for this step")
        return typed

    def _decimal(self, label: str, text: str) -> Decimal:
        try:
            value = Decimal(text.strip())
        except InvalidOperation:
            raise SmokeError(f"{label} is not a number") from None
        if not value.is_finite() or value <= 0:
            raise SmokeError(f"{label} must be positive")
        return value

    def preflight(self) -> None:
        body = self._call("GET", STATUS)
        execution = body.get("execution", {}) if isinstance(body, dict) else {}
        if execution.get("mode") != "practice" or execution.get("authority") is not True:
            raise SmokeError("the backend is not in practice mode with execution authority")
        self.say("Backend status: practice mode with authority.")

    def collect_readings(self) -> list[dict[str, str]]:
        readings = []
        for index in range(1, READINGS_PER_ORDER + 1):
            raw = self.ask(f"Reading {index} of {READINGS_PER_ORDER}: bid and ask from the practice app (e.g. 71.20 71.30): ")
            parts = raw.split()
            if len(parts) != 2:
                raise SmokeError("a reading is two numbers: bid then ask")
            bid, ask = self._decimal("bid", parts[0]), self._decimal("ask", parts[1])
            if bid > ask:
                raise SmokeError("a bid above the ask is not a reading")
            readings.append(
                {"bid": format(bid, "f"), "ask": format(ask, "f"), "at": self.clock().isoformat()}
            )
        return readings

    def _ticker(self) -> str:
        ticker = self.ask("Ticker exactly as Trading 212 lists it (e.g. VODl_EQ): ").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", ticker):
            raise SmokeError("that is not a Trading 212 ticker")
        return ticker

    def _whole(self, text: str) -> int:
        try:
            value = Decimal(text.strip())
        except InvalidOperation:
            raise SmokeError("quantity is not a number") from None
        if value != value.to_integral_value() or value <= 0:
            raise SmokeError("quantity must be a whole number of shares")
        return int(value)

    # -- one order step ----------------------------------------------------------------------------

    def _order(self, step: str, side: str, ticker: str, quantity: int, limit: Decimal,
               readings: list[dict[str, str]]) -> dict[str, Any]:
        phrase = f"{side} {ticker}"
        record = {
            "step": step, "ticker": ticker, "side": side, "quantity": quantity,
            "limit_price": format(limit, "f"), "readings": readings,
        }
        self.say(f"{side} {quantity} x {ticker}, LIMIT DAY at {limit}.")
        record["confirmation"] = self.confirm(phrase)
        payload = {
            "confirmation": "PREPARE_T212_PRACTICE", "ticker": ticker, "side": side,
            "quantity": quantity, "limit_price": format(limit, "f"),
            "readings": [
                {"bid": r["bid"], "ask": r["ask"], "observed_at": r["at"]} for r in readings
            ],
        }
        try:
            body = self._call("POST", PREPARE, payload)
        except SmokeError:
            record["admission"] = "REFUSED"
            self.evidence.add(record)
            raise
        admission = body.get("admission", {})
        record["admission"] = admission.get("decision")
        record["reason_code"] = admission.get("reason_code")
        record["proposal_id"] = body.get("proposal_id")
        if not body.get("admitted"):
            self.evidence.add(record)
            raise SmokeError(
                f"admission denied: {admission.get('reason_code')}. Stopped; nothing was retried."
            )
        self.say("Admitted. Approve it with Touch ID in the app (PRACTICE list). Waiting...")
        states = self._wait_for_approval(body["proposal_id"])
        record["states"] = states
        record["touch_id"] = "approved" if states and states[-1] not in NOT_SENT_STATES else "not approved"
        self.evidence.add(record)
        if states[-1] in NOT_SENT_STATES:
            raise SmokeError("no approval arrived in time; nothing was sent")
        if states[-1] in {"UNKNOWN", "FAILED", "REJECTED"}:
            raise SmokeError(f"the order ended {states[-1]}. Stopped; nothing was retried.")
        return record

    def _wait_for_approval(self, proposal_id: str) -> list[str]:
        states: list[str] = []
        for _ in range(self.approval_polls):
            body = self._call("GET", f"{PROPOSALS}/{proposal_id}")
            state = str(body.get("status"))
            if not states or states[-1] != state:
                states.append(state)
            if state not in NOT_SENT_STATES:
                return states
            self.sleep(self.poll_seconds)
        return states

    def _reconcile(self, proposal_id: str) -> dict[str, Any]:
        """Every reconcile goes through here, so every step is validated the same way."""

        result = self._call(
            "POST", RECONCILE, {"confirmation": "RECONCILE_T212_PRACTICE", "proposal_id": proposal_id}
        )
        self._check_reconcile(proposal_id, result)
        return result

    def _check_reconcile(self, proposal_id: str, result: Any) -> None:
        """Stop on any anomaly code or failed position check, and persist what was seen."""

        body = result if isinstance(result, dict) else {}
        code = str(body.get("code"))
        # A reconcile response always carries position_check. A missing value means the answer is
        # not the shape we expect, which is an anomaly and not a silent SKIPPED.
        raw_position_check = body.get("position_check")
        position_check = "MISSING" if raw_position_check is None else str(raw_position_check)
        problems = []
        if code not in HEALTHY_RECONCILE_CODES:
            problems.append(f"{body.get('state')}/{code}")
        if position_check not in HEALTHY_POSITION_CHECKS:
            problems.append(f"position check {position_check}")
        if not problems:
            return
        note = "; ".join(problems)
        anomaly = {"code": code, "position_check": position_check, "state": str(body.get("state"))}
        step_name = "unknown step"
        for step in self.evidence.steps:
            if step.get("proposal_id") == proposal_id and step.get("step") != "anomaly":
                step_name = str(step.get("step", "unknown step"))
                step["anomaly"] = anomaly
        self.evidence.add(
            {"step": "anomaly", "proposal_id": proposal_id, "result": str(body.get("state")), "note": note}
        )
        raise SmokeError(f"reconcile anomaly at {step_name} ({note}). Stopped; nothing further was sent.")

    def _reconcile_until(self, record: dict[str, Any], wanted: str) -> str:
        seen: list[str] = []
        state = ""
        for _ in range(self.reconcile_polls):
            result = self._reconcile(record["proposal_id"])
            state = str(result.get("state"))
            seen.append(f"{state}/{result.get('code')}")
            if state == wanted:
                break
            if state in TERMINAL_STATES:
                break
            self.sleep(self.reconcile_seconds)
        record["result"] = state
        record["reconciles"] = seen
        self.evidence.save()
        return state

    # -- the steps -----------------------------------------------------------------------------------

    def buy_far(self, index: int) -> dict[str, Any]:
        self.say(f"Far buy {index} of 3: three different tickers, each resting.")
        ticker = self._ticker()
        if ticker in self.far_tickers:
            raise SmokeError("the far buys use three different tickers (D-16)")
        quantity = self._whole(self.ask("Quantity (whole shares): "))
        readings = self.collect_readings()
        bid = Decimal(readings[-1]["bid"])
        limit = self._decimal("limit price", self.ask(f"Limit price (at most {FAR_FACTOR * bid} = 80% of the bid): "))
        if limit > FAR_FACTOR * bid:
            raise SmokeError("a far-from-market buy limit is at most 80% of the recorded bid")
        record = self._order(f"buy-{index}", "BUY", ticker, quantity, limit, readings)
        state = self._reconcile_once_resting(record)
        if state != "ACKNOWLEDGED":
            raise SmokeError(f"a far buy must stay resting; the order is {state}. Stopped.")
        self.far_tickers.append(ticker)
        return record

    def _reconcile_once_resting(self, record: dict[str, Any]) -> str:
        result = self._reconcile(record["proposal_id"])
        record["result"] = str(result.get("state"))
        record["reconciles"] = [f"{result.get('state')}/{result.get('code')}"]
        self.evidence.save()
        return record["result"]

    def cancel_one(self) -> dict[str, Any]:
        far = [s for s in self.evidence.steps if str(s.get("step", "")).startswith("buy-") and s.get("step") in ("buy-1", "buy-2", "buy-3") and s.get("result") == "ACKNOWLEDGED"]
        if not far:
            raise SmokeError("there is no resting far buy to cancel")
        names = ", ".join(s["ticker"] for s in far)
        self.say(f"Resting far buys: {names}. Cancel exactly one (no Touch ID: typed confirmation only, D-22).")
        ticker = self._ticker()
        target = next((s for s in far if s["ticker"] == ticker), None)
        if target is None:
            raise SmokeError("that ticker is not one of the resting far buys")
        record = {"step": "cancel", "ticker": ticker, "proposal_id": target["proposal_id"]}
        record["confirmation"] = self.confirm(f"CANCEL {ticker}")
        body = self._call(
            "POST", CANCEL, {"confirmation": "CANCEL_T212_PRACTICE", "proposal_id": target["proposal_id"]}
        )
        record["note"] = f"cancel {body.get('cancel', {}).get('outcome')}"
        self.evidence.add(record)
        state = self._reconcile_until(record, "CANCELLED")
        if state != "CANCELLED":
            raise SmokeError(f"the cancelled order is {state} after the bounded reconcile. Stopped.")
        target["result"] = "CANCELLED"
        self.evidence.save()
        return record

    def buy_q1(self) -> dict[str, Any]:
        self.say("Q1 buy: ONE whole share on a fourth ticker at a marketable limit (D-27).")
        ticker = self._ticker()
        if ticker in self.far_tickers or any(
            s.get("ticker") == ticker for s in self.evidence.steps if str(s.get("step", "")).startswith("buy-")
        ):
            raise SmokeError("the Q1 ticker must differ from the three far buys")
        quantity = self._whole(self.ask("Quantity (must be 1): "))
        if quantity != 1:
            raise SmokeError("the Q1 buy is exactly one share")
        readings = self.collect_readings()
        ask = Decimal(readings[-1]["ask"])
        limit = self._decimal("limit price", self.ask(f"Limit price (at or above the recorded ask {ask}): "))
        if limit < ask:
            raise SmokeError("a limit below the recorded ask is a resting order, not the Q1 buy")
        record = self._order("buy-q1", "BUY", ticker, quantity, limit, readings)
        state = self._reconcile_until(record, "FILLED")
        if state != "FILLED":
            raise SmokeError(f"the Q1 buy is {state} after the bounded reconcile. Stopped.")
        return record

    def sell_q1(self) -> dict[str, Any]:
        q1 = self.evidence.step_for("buy-q1")
        if q1 is None or not q1.get("proposal_id"):
            raise SmokeError("there is no Q1 buy to sell")
        state = str(self._reconcile(q1["proposal_id"]).get("state"))
        if state != "FILLED":
            raise SmokeError("the Q1 position is not reconciled as filled; the sell is refused")
        ticker = q1["ticker"]
        self.say(f"Sell the Q1 share: exactly 1 x {ticker}.")
        typed = self._ticker()
        if typed != ticker:
            raise SmokeError("the sell is for the Q1 ticker only")
        quantity = self._whole(self.ask("Quantity (must be 1): "))
        if quantity != 1:
            raise SmokeError("the sell is exactly the one Q1 share")
        readings = self.collect_readings()
        bid = Decimal(readings[-1]["bid"])
        limit = self._decimal("limit price", self.ask(f"Limit price (at or below the recorded bid {bid}): "))
        if limit > bid:
            raise SmokeError("a limit above the recorded bid is a resting order, not a marketable sell")
        record = self._order("sell", "SELL", ticker, quantity, limit, readings)
        state = self._reconcile_until(record, "FILLED")
        if state != "FILLED":
            raise SmokeError(f"the sell is {state} after the bounded reconcile. Stopped.")
        return record

    def reconcile_all(self) -> list[dict[str, Any]]:
        remaining = [
            s for s in self.evidence.steps
            if s.get("step") in ("buy-1", "buy-2", "buy-3") and s.get("result") == "ACKNOWLEDGED"
        ]
        if not remaining:
            raise SmokeError("no resting far buy is recorded")
        names = ", ".join(s["ticker"] for s in remaining)
        self.say(f"Remaining far buys: {names}. This only reads, through the backend.")
        self.confirm("RECONCILE ALL")
        reports = []
        for step in remaining:
            result = self._reconcile(step["proposal_id"])
            report = {
                "step": f"reconcile-{step['step']}", "ticker": step["ticker"],
                "result": str(result.get("state")), "note": f"code {result.get('code')}",
            }
            self.evidence.add(report)
            reports.append(report)
            self.say(f"{step['ticker']}: {report['result']} ({result.get('code')})")
        return reports

    def run(self) -> None:
        self.preflight()
        for index in (1, 2, 3):
            self.buy_far(index)
        self.cancel_one()
        self.buy_q1()
        self.sell_q1()
        self.say("Smoke steps done. Next trading day: run the reconcile-all command.")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Operator-run Trading 212 practice smoke (66-05)")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "reconcile-all"])
    parser.add_argument("--backend", default=DEFAULT_BACKEND)
    parser.add_argument("--evidence", default=str(DEFAULT_EVIDENCE), help="evidence path stem (.md and .json)")
    args = parser.parse_args(argv)
    try:
        client = LoopbackClient(args.backend)
    except SmokeError as exc:
        print(f"refused: {exc}")
        return 2
    evidence = Evidence(Path(args.evidence))
    evidence.load()
    smoke = Smoke(client, input, print, evidence)
    try:
        if evidence.account_last4 == "????":
            evidence.account_last4 = last_four(
                input("Practice account id (only the last four characters are kept): ")
            )
            evidence.save()
        if args.command == "reconcile-all":
            smoke.preflight()
            smoke.reconcile_all()
        else:
            smoke.run()
    except SmokeError as exc:
        print(f"STOPPED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
