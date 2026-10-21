"""Operator probe for the intraday route (64-02 Task 2, design section 11).

``step`` runs one checklist step (S-3 to S-10, S-12) against a live relay; ``compare`` (S-11b) is
offline. Every charged request (intraday bars, quotes, daily bars, first try or retry, any outcome)
goes through one Mac-side ledger and one stop rule, so the probe can never spend more than 20 Breeze
calls in a day:

* S-3 stores ``baseline = calls_today_ist`` in ``DIR/ledger-<ist date>.json``. Later runs on that
  date reuse it, so re-running a step cannot reset the count.
* Before EVERY charged request the tool reads ``/v1/budget`` (free) and sends only if
  ``max(ledger_sent, calls_today_ist - baseline) + 1 <= 20``. A retry (one retry after an empty
  answer or a 5xx, or a repeat of a step already run) also needs the base slots of the slots not
  yet run: 14 base calls, 6 retry slots. S-8 is never retried.
* A base call that cannot be sent is recorded ``probe_budget_stop`` and the tool sends nothing
  more that day. A retry that cannot be sent is recorded ``probe_retry_skipped`` and keeps the
  first answer; it does not stop later base steps.
* The ledger is written before the request goes out, so a crash leaves the call counted.

Saved answers stay on the Mac under ``DIR/answers/<ist date>/`` (exact bytes, the C4 headers and
the sha256). Nothing here writes to the VM, and nothing printed carries a token, an id or the login
URL. No order route and no ``preview_order`` is reachable from this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import UpstreamPayload
from .errors import RelayError, UpstreamError, UpstreamTimeout

HARD_STOP = 20
BASE_CALLS = 14
QUOTE_GAP_S = 60.0
CAPPED_ROWS = 1000
LEDGER_SCHEMA = "growin.probe.ledger/1"
ANSWER_SCHEMA = "growin.probe.answer/1"
PROBE_SYMBOLS = ("RELIND", "BAJFI", "NIFBEE")
S5_SYMBOL = "RELIND"
S8_SYMBOL = "RELIND"
S10_SYMBOLS = ("RELIND", "NIFBEE")

STEPS = ("S-3", "S-4", "S-5", "S-6", "S-7", "S-8", "S-9", "S-10", "S-12")
CHARGED_STEPS = ("S-3", "S-4", "S-5", "S-6", "S-7", "S-8", "S-9", "S-10")
BASE_SLOTS: Mapping[str, int] = {
    "S-3": 3,
    "S-4": 1,
    "S-5": 2,
    "S-6": 3,
    "S-7": 1,
    "S-8": 1,
    "S-9": 1,
    "S-10": 2,
}
assert sum(BASE_SLOTS.values()) == BASE_CALLS
NEVER_RETRIED = frozenset({"S-8"})

BUDGET_STOP = "probe_budget_stop"
RETRY_SKIPPED = "probe_retry_skipped"

U23_NOTE = (
    "U-23: pre-open and block-deal window volume can sit in bhavcopy TTQ outside D, so a shortfall "
    "stays unattributed until S-1 records whether TTQ includes it"
)
IST = timezone(timedelta(hours=5, minutes=30))
CLOSE_FETCH_FROM = time(15, 40)
_CLOCK = re.compile(r"(\d{2}):(\d{2})")
_SLOT_SAFE = re.compile(r"[^A-Za-z0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProbeError(Exception):
    """A refusal before anything was sent. ``code`` is a closed word, never a value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EvidenceError(Exception):
    """Saved evidence failed a check (hash mismatch, missing answers). ``code`` is a closed word."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Slot:
    key: str
    kind: str  # intraday | quote | daily
    symbol: str
    day: date


# ------------------------------------------------------------------------- bodies


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _success(body: bytes) -> Any:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload.get("Success") if isinstance(payload, dict) else None


def _clock_of(label: object) -> tuple[int, int] | None:
    if not isinstance(label, str):
        return None
    match = _CLOCK.search(label)
    return (int(match.group(1)), int(match.group(2))) if match else None


def summarise_bars(body: bytes, window: tuple[time, time]) -> dict[str, Any] | None:
    """Facts about one minute-bar body, or None when it has no ``Success`` list."""
    rows = _success(body)
    if not isinstance(rows, list):
        return None
    start, end = (window[0].hour, window[0].minute), (window[1].hour, window[1].minute)
    volume_all = volume_day = 0
    zero = single = bad = 0
    after = {"1514": 0, "1515": 0, "1529": 0, "1535": 0}
    high: Decimal | None = None
    low: Decimal | None = None
    labels: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            bad += 1
            continue
        label = row.get("datetime")
        volume = _decimal(row.get("volume"))
        row_high, row_low = _decimal(row.get("high")), _decimal(row.get("low"))
        if not isinstance(label, str) or volume is None or row_high is None or row_low is None:
            bad += 1
            continue
        labels.append(label)
        count = int(volume)
        volume_all += count
        clock = _clock_of(label)
        if clock is not None:
            if start <= clock <= end:
                volume_day += count
            for name, limit in (("1514", (15, 14)), ("1515", (15, 15)), ("1529", (15, 29)), ("1535", (15, 35))):
                if clock > limit:
                    after[name] += 1
        if count == 0:
            zero += 1
        if row_high == row_low:
            single += 1
        high = row_high if high is None or row_high > high else high
        low = row_low if low is None or row_low < low else low
    return {
        "rows": len(rows),
        "bad_rows": bad,
        "first_label": labels[0] if labels else None,
        "last_label": labels[-1] if labels else None,
        "volume_all": volume_all,
        "volume_day_window": volume_day,
        "after": after,
        "zero_volume": zero,
        "single_price": single,
        "max_high": high,
        "min_low": low,
        "capped": len(rows) >= CAPPED_ROWS,
    }


def _text(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 101.0000 and 101 print the same
    return str(value)


def _bars_line(step: str, slot: Slot, seq: int, payload: UpstreamPayload, window: tuple[time, time]) -> str:
    facts = summarise_bars(payload.body, window)
    head = f"answer {step} {slot.kind} {slot.symbol} seq {seq} empty {_flag(payload.empty)}"
    if facts is None:
        return f"{head} rows unknown sha256 verified"
    after = facts["after"]
    if slot.kind == "daily":
        today = [
            row
            for row in (_success(payload.body) or [])
            if isinstance(row, dict) and str(row.get("datetime", "")).startswith(slot.day.isoformat())
        ]
        volume = _text(_decimal(today[0].get("volume"))) if today else "none"
        return f"{head} rows {facts['rows']} today_row {_flag(bool(today))} today_volume {volume} sha256 verified"
    return (
        f"{head} rows {facts['rows']} first_label {json.dumps(facts['first_label'])} "
        f"last_label {json.dumps(facts['last_label'])} capped {_flag(facts['capped'])} "
        f"bad_rows {facts['bad_rows']} volume_sum_all {facts['volume_all']} "
        f"volume_sum_day_window {facts['volume_day_window']} after_1514 {after['1514']} "
        f"after_1515 {after['1515']} after_1529 {after['1529']} after_1535 {after['1535']} "
        f"zero_volume_rows {facts['zero_volume']} single_price_rows {facts['single_price']} "
        f"max_high {_text(facts['max_high'])} min_low {_text(facts['min_low'])} sha256 verified"
    )


def _quote_line(step: str, slot: Slot, seq: int, payload: UpstreamPayload) -> str:
    rows = _success(payload.body)
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    values = " ".join(
        f"{key} {json.dumps(row.get(key)) if key == 'ltt' else _text(row.get(key))}"
        for key in ("ltt", "total_quantity_traded", "high", "low")
    )
    return f"answer {step} quote {slot.symbol} seq {seq} empty {_flag(payload.empty)} {values} sha256 verified"


def _flag(value: bool) -> str:
    return "true" if value else "false"


# ------------------------------------------------------------------------- ledger


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Ledger:
    """The Mac-side count of charged requests for one IST probe date."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    @classmethod
    def path_for(cls, probe_dir: Path, probe_date: date) -> Path:
        return probe_dir / f"ledger-{probe_date.isoformat()}.json"

    @classmethod
    def load(cls, probe_dir: Path, probe_date: date) -> "Ledger | None":
        path = cls.path_for(probe_dir, probe_date)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ProbeError("ledger_unreadable") from None
        ok = (
            isinstance(data, dict)
            and data.get("schema") == LEDGER_SCHEMA
            and data.get("probe_date") == probe_date.isoformat()
            and isinstance(data.get("baseline"), int)
            and isinstance(data.get("sent"), int)
            and isinstance(data.get("stopped"), bool)
            and isinstance(data.get("steps"), dict)
        )
        if not ok:
            raise ProbeError("ledger_invalid")
        return cls(path, data)

    @classmethod
    def create(cls, probe_dir: Path, probe_date: date, baseline: int) -> "Ledger":
        ledger = cls(
            cls.path_for(probe_dir, probe_date),
            {
                "schema": LEDGER_SCHEMA,
                "probe_date": probe_date.isoformat(),
                "baseline": baseline,
                "sent": 0,
                "stopped": False,
                "steps": {},
            },
        )
        ledger.save()
        return ledger

    def save(self) -> None:
        _atomic_write(self.path, (json.dumps(self.data, sort_keys=True, indent=2) + "\n").encode("utf-8"))

    @property
    def baseline(self) -> int:
        return self.data["baseline"]

    @property
    def sent(self) -> int:
        return self.data["sent"]

    @property
    def stopped(self) -> bool:
        return self.data["stopped"]

    def slot(self, step: str, key: str) -> dict[str, Any]:
        slots = self.data["steps"].setdefault(step, {}).setdefault("slots", {})
        return slots.setdefault(key, {"requests": 0, "result": "not_run"})

    def unsent_base_slots(self) -> int:
        remaining = 0
        for step, base in BASE_SLOTS.items():
            slots = self.data["steps"].get(step, {}).get("slots", {})
            attempted = sum(1 for record in slots.values() if record["requests"] > 0)
            remaining += max(0, base - attempted)
        return remaining


# ------------------------------------------------------------------------- step


def _slots(step: str, today: date, symbol: str | None, holiday: date | None) -> list[Slot]:
    if step == "S-3" or step == "S-6":
        return [Slot(f"intraday-{code}", "intraday", code, today) for code in PROBE_SYMBOLS]
    if step in ("S-4", "S-9"):
        return [Slot(f"intraday-{S5_SYMBOL}", "intraday", S5_SYMBOL, today)]
    if step == "S-5":
        return [Slot(f"quote-{S5_SYMBOL}-{n}", "quote", S5_SYMBOL, today) for n in (1, 2)]
    if step == "S-7":
        if not symbol:
            raise ProbeError("probe_symbol_required")
        return [Slot(f"intraday-{symbol}", "intraday", symbol, today)]
    if step == "S-8":
        if holiday is None:
            raise ProbeError("probe_holiday_required")
        if holiday >= today:
            raise ProbeError("probe_holiday_not_past")
        return [Slot(f"intraday-{S8_SYMBOL}-holiday", "intraday", S8_SYMBOL, holiday)]
    if step == "S-10":
        return [Slot(f"daily-{code}", "daily", code, today) for code in S10_SYMBOLS]
    raise ProbeError("probe_step_unknown")


def _file_stem(seq: int, step: str, slot: Slot) -> str:
    return f"{seq:03d}-{step}-{_SLOT_SAFE.sub('_', slot.key)}"


class IntradayProbe:
    """Runs checklist steps against a relay-shaped client (``budget``, ``intraday_bars``, ``quote``, ``daily_bars``)."""

    def __init__(
        self,
        relay: Any,
        probe_dir: Path,
        say: Callable[[str], None],
        *,
        sleep: Callable[[float], None],
        window: tuple[time, time],
    ) -> None:
        self.relay = relay
        self.probe_dir = Path(probe_dir)
        self.say = say
        self.sleep = sleep
        self.window = window
        self.ledger: Ledger
        self.probe_date: date

    # -- entry point

    def run(self, step: str, *, symbol: str | None = None, holiday: date | None = None) -> int:
        """Run one step. Returns 0; raises ProbeError before sending or RelayError after recording."""
        if step not in STEPS:
            raise ProbeError("probe_step_unknown")
        budget = self.relay.budget()  # free read: gives the IST date and the S-3 baseline
        self.probe_date = budget.ist_date
        existing = Ledger.load(self.probe_dir, self.probe_date)
        if step == "S-3" and existing is None:
            existing = Ledger.create(self.probe_dir, self.probe_date, budget.calls_today_ist)
        if existing is None:
            raise ProbeError("probe_baseline_missing")
        self.ledger = existing
        if step == "S-12":
            return self._reconcile(budget.calls_today_ist)
        slots = _slots(step, self.probe_date, symbol, holiday)
        for index, slot in enumerate(slots):
            if slot.kind == "quote" and index > 0 and not self.ledger.stopped:
                self.sleep(QUOTE_GAP_S)
            self._run_slot(step, slot)
        statuses = {self.ledger.slot(step, slot.key)["result"] for slot in slots}
        status = (
            BUDGET_STOP
            if BUDGET_STOP in statuses
            else RETRY_SKIPPED
            if RETRY_SKIPPED in statuses
            else "complete"
        )
        self.say(f"step {step} status {status} sent {self.ledger.sent}")
        return 0

    # -- the stop rule

    def _allowed(self, kind: str) -> bool:
        """True when one more charged request of ``kind`` ("base" or "retry") fits under the hard stop."""
        if self.ledger.stopped:
            return False
        budget = self.relay.budget()  # free read before EVERY charged request
        if budget.ist_date != self.probe_date:
            raise ProbeError("probe_date_changed")
        spent = max(self.ledger.sent, budget.calls_today_ist - self.ledger.baseline)
        need = 1 if kind == "base" else 1 + self.ledger.unsent_base_slots()
        return spent + need <= HARD_STOP

    def _run_slot(self, step: str, slot: Slot) -> None:
        record = self.ledger.slot(step, slot.key)
        kind = "base" if record["requests"] == 0 else "retry"  # a repeated step spends retry slots
        retried = False
        while True:
            if not self._allowed(kind):
                result = BUDGET_STOP if kind == "base" else RETRY_SKIPPED
                if kind == "base":
                    self.ledger.data["stopped"] = True  # nothing more is sent today
                record["result"] = result
                self.ledger.save()
                self.say(f"slot {step} {slot.kind} {slot.symbol} {result}")
                return
            # Durable before acting: a crash after this line leaves the call counted.
            self.ledger.data["sent"] += 1
            record["requests"] += 1
            seq = self.ledger.sent
            self.ledger.save()
            try:
                payload = self._fetch(slot)
            except (UpstreamError, UpstreamTimeout) as err:
                record["result"] = f"error_{err.code}"
                self.ledger.save()
                self.say(f"slot {step} {slot.kind} {slot.symbol} error {err.code}")
                if step in NEVER_RETRIED or retried:
                    return
                retried, kind = True, "retry"
                continue
            except RelayError as err:
                record["result"] = f"error_{err.code}"
                self.ledger.save()
                raise
            self._save_answer(step, slot, seq, payload)
            record["result"] = "empty" if payload.empty else "answered"
            self.ledger.save()
            self._print_answer(step, slot, seq, payload)
            if payload.empty and step not in NEVER_RETRIED and not retried:
                retried, kind = True, "retry"
                continue
            return

    def _fetch(self, slot: Slot) -> UpstreamPayload:
        if slot.kind == "intraday":
            return self.relay.intraday_bars(slot.symbol, slot.day)
        if slot.kind == "quote":
            return self.relay.quote(slot.symbol)
        return self.relay.daily_bars(slot.symbol, slot.day, slot.day)

    # -- evidence

    def _answers_dir(self) -> Path:
        return self.probe_dir / "answers" / self.probe_date.isoformat()

    def _save_answer(self, step: str, slot: Slot, seq: int, payload: UpstreamPayload) -> None:
        stem = _file_stem(seq, step, slot)
        meta = {
            "schema": ANSWER_SCHEMA,
            "step": step,
            "slot": slot.key,
            "seq": seq,
            "symbol": slot.symbol,
            "kind": slot.kind,
            "session_date": slot.day.isoformat(),
            "provenance": payload.provenance(),
            "upstream_interval": payload.upstream_interval,
            "breeze_status": payload.breeze_status,
            "empty": payload.empty,
            "upstream_last_modified": payload.upstream_last_modified,
            "sha256": payload.sha256,
        }
        directory = self._answers_dir()
        _atomic_write(directory / f"{stem}.body", payload.body)
        _atomic_write(directory / f"{stem}.meta.json", (json.dumps(meta, sort_keys=True, indent=2) + "\n").encode())

    def _print_answer(self, step: str, slot: Slot, seq: int, payload: UpstreamPayload) -> None:
        if slot.kind == "quote":
            self.say(_quote_line(step, slot, seq, payload))
        else:
            self.say(_bars_line(step, slot, seq, payload, self.window))

    def _reconcile(self, calls_today: int) -> int:
        delta = calls_today - self.ledger.baseline
        self.say(f"step S-12 relay_delta {delta} ledger_sent {self.ledger.sent}")
        self.say(f"step S-12 counts {'match' if delta == self.ledger.sent else 'DIFFER'}")
        return 0


# ------------------------------------------------------------------------- compare


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EvidenceError("saved_answer_unreadable") from None
    if not isinstance(data, dict) or data.get("schema") != ANSWER_SCHEMA:
        raise EvidenceError("saved_answer_invalid")
    return data


def _latest_answer_date(saved: Path) -> date:
    root = saved / "answers"
    if not root.is_dir():
        raise EvidenceError("saved_answers_missing")
    days = []
    for child in root.iterdir():
        try:
            days.append(date.fromisoformat(child.name))
        except ValueError:
            continue
    if not days:
        raise EvidenceError("saved_answers_missing")
    return max(days)


def saved_s6_answers(saved: Path, probe_date: date) -> dict[str, tuple[dict[str, Any], bytes]]:
    """Per symbol, the highest-seq saved S-6 answer with its exact bytes, each re-hashed against its sha256."""
    directory = saved / "answers" / probe_date.isoformat()
    chosen: dict[str, tuple[dict[str, Any], Path]] = {}
    for meta_path in sorted(directory.glob("*.meta.json")) if directory.is_dir() else []:
        meta = _read_json(meta_path)
        if meta.get("step") != "S-6" or meta.get("kind") != "intraday":
            continue
        symbol = meta.get("symbol")
        seq = meta.get("seq")
        if not isinstance(symbol, str) or not isinstance(seq, int):
            raise EvidenceError("saved_answer_invalid")
        if symbol not in chosen or seq > chosen[symbol][0]["seq"]:
            chosen[symbol] = (meta, meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".body"))
    if not chosen:
        raise EvidenceError("saved_s6_missing")
    answers: dict[str, tuple[dict[str, Any], bytes]] = {}
    for symbol, (meta, body_path) in chosen.items():
        try:
            body = body_path.read_bytes()
        except OSError:
            raise EvidenceError("saved_body_missing") from None
        recorded = meta.get("sha256")
        if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
            raise EvidenceError("saved_answer_invalid")
        if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), recorded):
            raise EvidenceError("saved_hash_mismatch")
        answers[symbol] = (meta, body)
    return answers


def compare(
    saved: Path,
    pilot_store: Path,
    say: Callable[[str], None],
    *,
    window: tuple[time, time],
    probe_date: date | None = None,
) -> int:
    """S-11b. Offline: reads the saved S-6 bytes, the local pilot store and the committed CAS table.

    It builds no RelayClient and no HTTP client, and it opens the pilot store read-only.
    """
    from costs.cas_class import CasClassUnavailable, resolve_cas_class
    from pilot_data.bhavcopy import primary_bars_on
    from pilot_data.core import PilotDataError
    from pilot_data.security_master import latest_snapshot, lookup_stock_code
    from pilot_data.store import PilotDataStore

    day = probe_date or _latest_answer_date(saved)
    answers = saved_s6_answers(saved, day)  # re-hashes every saved body before anything is read
    say(f"compare probe_date {day.isoformat()} symbols {len(answers)} saved_hashes verified")
    try:
        store = PilotDataStore(pilot_store, workspace="india", read_only=True)
    except PilotDataError as err:
        raise EvidenceError(f"pilot_store_{err.code}") from None
    with store:
        try:
            bars = primary_bars_on(store, day)
            snapshot = latest_snapshot(store, on_or_before=day)
        except PilotDataError as err:
            raise EvidenceError(f"pilot_store_{err.code}") from None
        for symbol in sorted(answers):
            meta, body = answers[symbol]
            facts = summarise_bars(body, window)
            fetched = datetime.fromisoformat(meta["provenance"]["fetched_at_utc"].replace("Z", "+00:00"))
            fetched_ist = fetched.astimezone(IST)
            say(
                f"symbol {symbol} sha256 verified fetched_at_ist {fetched_ist.isoformat(timespec='seconds')} "
                f"after_1540_ist {_flag(fetched_ist.timetz().replace(tzinfo=None) >= CLOSE_FETCH_FROM)}"
            )
            if facts is None:
                say(f"symbol {symbol} bars_unreadable")
                continue
            try:
                master = lookup_stock_code(store, snapshot, symbol) if snapshot is not None else None
            except PilotDataError as err:
                say(f"symbol {symbol} master_lookup {err.code}")
                continue
            if master is None or not master.isin:
                say(f"symbol {symbol} bhavcopy_row none reason master_unmapped")
                continue
            rows = [bar for bar in bars if bar.isin == master.isin and bar.series in ("EQ", "BE")]
            if len(rows) != 1:
                say(f"symbol {symbol} bhavcopy_row none reason {'ambiguous' if rows else 'absent'}")
                continue
            bhavcopy = rows[0]
            try:
                cas = resolve_cas_class(master.isin, day)
                klass = f"class {cas.cas_class} table_version {cas.table_version}"
            except CasClassUnavailable:
                klass = "class session_scope_unknown table_version none"
            if bhavcopy.volume > 0:
                ratio = (Decimal(facts["volume_day_window"]) / Decimal(bhavcopy.volume)).quantize(Decimal("0.0001"))
            else:
                ratio = None
            say(
                f"symbol {symbol} bhavcopy_ttq {bhavcopy.volume} bar_volume_sum_day_window "
                f"{facts['volume_day_window']} ratio {'none' if ratio is None else f'{ratio:.4f}'} {klass} note {json.dumps(U23_NOTE)}"
            )
            high, low = facts["max_high"], facts["min_low"]
            say(
                f"symbol {symbol} day_high bhavcopy {_text(bhavcopy.high)} bars {_text(high)} "
                f"match {_flag(high is not None and high == bhavcopy.high)}"
            )
            say(
                f"symbol {symbol} day_low bhavcopy {_text(bhavcopy.low)} bars {_text(low)} "
                f"match {_flag(low is not None and low == bhavcopy.low)}"
            )
    return 0


def run_step(
    relay: Any,
    probe_dir: Path,
    step: str,
    say: Callable[[str], None],
    *,
    sleep: Callable[[float], None],
    window: tuple[time, time],
    symbol: str | None = None,
    holiday: date | None = None,
) -> int:
    return IntradayProbe(relay, probe_dir, say, sleep=sleep, window=window).run(
        step, symbol=symbol, holiday=holiday
    )


def day_window() -> tuple[time, time]:
    """The R-15 day window of the committed schedule, so the probe sums the same minutes the simulator does."""
    from costs.intraday_fills import load_intraday_scenarios

    schedule = load_intraday_scenarios()
    return schedule.day_window_start, schedule.day_window_end


__all__: Sequence[str] = (
    "BASE_CALLS",
    "BASE_SLOTS",
    "BUDGET_STOP",
    "EvidenceError",
    "HARD_STOP",
    "IntradayProbe",
    "Ledger",
    "ProbeError",
    "RETRY_SKIPPED",
    "STEPS",
    "compare",
    "day_window",
    "run_step",
    "summarise_bars",
)
