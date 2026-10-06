"""mint and authorize over injected ports (O2, O5).

The pipeline owns only the order of operations. Everything it consults arrives
through a port: the kill switch, risk and account reads, and the rule
evaluator sit behind one Guard; consumed intent ids behind an IntentLedger;
time behind a clock. Each Guard.check performs fresh reads (P-04): it is
called once at mint and again at authorize, and nothing is cached between.

In Phase 63 the forward step is a constant refusal. The pipeline never calls
it and never builds a broker request: a verified intent is audited and the
answer is VERIFIED_NOT_FORWARDED. RefusalForward exists so a test can prove the
call count stays zero on every path.
"""

from __future__ import annotations

import base64
import re
import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol, Sequence

from . import (
    CONTRACT,
    MAX_OUTSTANDING_CHALLENGES,
    OrderRefusal,
    intent_invalid,
    refusal,
)
from .audit import AuditBroken, AuditLog
from .challenge import ChallengeStore, SecretIds, intent_sha256
from .intent import Intent, parse_intent
from .kill import KillState
from .limits import (
    AccountSnapshot,
    Limits,
    Quote,
    TickTable,
    evaluate,
    session_open,
    to_ist,
)
from .risk import apply_trades, detect_mismatch
from .store import StateStore
from .verify import PinnedKey

Clock = Callable[[], datetime]

_CHALLENGE_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


@dataclass(frozen=True)
class GuardResult:
    """Outcome of one fresh check. Empty codes means the intent may proceed."""

    codes: tuple[str, ...] = ()
    kill: str | None = "enabled"
    latches: tuple[str, ...] = ()


class Guard(Protocol):
    def check(self, intent: Intent, now: datetime) -> GuardResult: ...

    def set_mac_halt(self) -> tuple[str, ...]: ...


class IntentLedger(Protocol):
    def is_consumed(self, intent_id: str) -> bool: ...

    def consume(self, intent_id: str) -> None: ...


class RefusalForward:
    """The only forward implementation in 63: a constant refusal that counts calls.

    The pipeline does not call it. A test asserts ``calls == 0`` after every
    path, so any future code that starts forwarding trips them.
    """

    def __init__(self) -> None:
        self.calls = 0

    def refuse(self, intent_id: str) -> str:
        self.calls += 1
        return "VERIFIED_NOT_FORWARDED"


@dataclass(frozen=True)
class MintResult:
    challenge_id: str
    signed_bytes: bytes
    expires_at: int

    @property
    def signed_bytes_b64(self) -> str:
        return base64.b64encode(self.signed_bytes).decode("ascii")

    @property
    def expires_at_utc(self) -> str:
        return datetime.fromtimestamp(self.expires_at, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def body(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "challenge_id": self.challenge_id,
            "signed_bytes_b64": self.signed_bytes_b64,
            "expires_at_utc": self.expires_at_utc,
        }


@dataclass(frozen=True)
class AuthorizeResult:
    decision: str
    intent_id: str
    audit_seq: int
    audit_sha256: str

    def body(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "decision": self.decision,
            "intent_id": self.intent_id,
            "audit_seq": self.audit_seq,
            "audit_sha256": self.audit_sha256,
        }


class OrderPipeline:
    def __init__(
        self,
        *,
        key: PinnedKey,
        limits_sha256: str,
        intents: IntentLedger,
        guard: Guard,
        audit: AuditLog,
        clock: Clock,
        forward: RefusalForward,
        ids: SecretIds | None = None,
        lock: AbstractContextManager[Any] | None = None,
    ) -> None:
        if type(forward) is not RefusalForward:
            raise TypeError("63 forwards nothing: only RefusalForward is accepted")
        self._key = key
        self._limits_sha256 = limits_sha256
        self._intents = intents
        self._guard = guard
        self._audit = audit
        self._clock = clock
        self._forward = forward
        self._ids = ids or SecretIds()
        self._challenges = ChallengeStore()
        self._lock: AbstractContextManager[Any] = lock or threading.RLock()

    # -- helpers -----------------------------------------------------------

    def _now(self) -> tuple[datetime, int]:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("pipeline clock must be timezone-aware")
        return now, int(now.timestamp())

    def _late_codes(self, now: datetime, expires_at: int | None = None) -> list[str]:
        """Codes for a deadline that passed while the reads were in flight.

        ``now`` must be sampled AFTER the blocking reads and the audit write: a
        check that began at 15:09:59 and finished at 15:10:01 is a 15:10:01
        check (D-11), and a challenge is only good while the VM clock says so.
        """
        codes: list[str] = []
        if not session_open(to_ist(now)):
            codes.append("session_closed")
        if expires_at is not None and int(now.timestamp()) >= expires_at:
            codes.append("challenge_expired")
        return codes

    def _entry(
        self,
        route: str,
        intent: Intent | None,
        decision: str,
        codes: Sequence[str],
        result: GuardResult | None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "route": route,
            "decision": decision,
            "codes": list(codes),
            "limits_sha256": self._limits_sha256,
            "key_id": self._key.key_id,
            "kill": result.kill if result else None,
            "latches": list(result.latches) if result else [],
        }
        if intent is not None:
            fields.update(
                intent_id=intent.intent_id,
                proposal_id=intent.proposal_id,
                intent_sha256=intent_sha256(intent),
                side=intent.side,
                stock_code=intent.stock_code,
                isin=intent.isin,
                quantity=intent.quantity,
                limit_price=intent.limit_price,
                reason=intent.reason,
                batch_id=intent.batch_id,
            )
        return fields

    def _append(self, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._audit.append(fields)
        except AuditBroken:
            raise refusal("audit_broken") from None

    def _refuse(
        self,
        route: str,
        intent: Intent | None,
        codes: Sequence[str],
        result: GuardResult | None = None,
    ) -> OrderRefusal:
        """Audit a REFUSED entry (all codes) and return the primary refusal.

        If the audit cannot be written the answer is audit_broken instead: an
        order that cannot be recorded is never answered with its own reason.
        """
        self._append(self._entry(route, intent, "REFUSED", codes, result))
        return refusal(codes[0])

    # -- routes ------------------------------------------------------------

    def mint(self, raw_body: bytes | str) -> MintResult:
        intent = parse_intent(raw_body)
        with self._lock:
            now, epoch = self._now()
            if intent.limits_sha256 != self._limits_sha256:
                raise self._refuse("intents", intent, ["limits_hash_mismatch"])
            if intent.key_id != self._key.key_id:
                raise self._refuse("intents", intent, ["key_mismatch"])
            if self._intents.is_consumed(intent.intent_id):
                raise self._refuse("intents", intent, ["intent_consumed"])
            if self._challenges.has_live_for_intent(intent.intent_id, epoch):
                raise self._refuse("intents", intent, ["challenge_outstanding"])
            if self._challenges.live_count(epoch) >= MAX_OUTSTANDING_CHALLENGES:
                raise self._refuse("intents", intent, ["challenge_capacity"])
            result = self._guard.check(intent, now)
            if result.codes:
                raise self._refuse("intents", intent, result.codes, result)
            # The guard's reads are done and may have been slow. Sample the VM
            # clock again: the cutoff is judged, and the challenge is issued, at
            # the time the reads finished, not the time the request arrived.
            now, epoch = self._now()
            late = self._late_codes(now)
            if late:
                raise self._refuse("intents", intent, late, result)
            challenge = self._challenges.mint(
                intent,
                now_epoch=epoch,
                key_id=self._key.key_id,
                limits_sha256=self._limits_sha256,
                ids=self._ids,
            )
            try:
                self._append(self._entry("intents", intent, "CHALLENGED", [], result))
            except OrderRefusal:
                self._challenges.take(challenge.challenge_id)
                raise
            # Last look, immediately before success is returned: the audit
            # write is also blocking. A challenge that is already dead or past
            # the cutoff is never handed out.
            final, _ = self._now()
            late = self._late_codes(final, challenge.expires_at)
            if late:
                self._challenges.take(challenge.challenge_id)
                raise self._refuse("intents", intent, late, result)
            return MintResult(
                challenge_id=challenge.challenge_id,
                signed_bytes=challenge.signed_bytes,
                expires_at=challenge.expires_at,
            )

    def authorize(self, challenge_id: str, signature_der: bytes) -> AuthorizeResult:
        if not isinstance(challenge_id, str) or _CHALLENGE_ID.fullmatch(challenge_id) is None:
            raise intent_invalid("bad_challenge_id")
        if not isinstance(signature_der, (bytes, bytearray)):
            raise intent_invalid("bad_signature_type")
        with self._lock:
            now, epoch = self._now()
            # O5: known, then clock, then consume (single use), then verify.
            challenge = self._challenges.take(challenge_id)
            intent = challenge.intent
            if epoch >= challenge.expires_at:
                raise self._refuse("authorize", intent, ["challenge_expired"])
            if not self._key.verify_der(bytes(signature_der), challenge.signed_bytes):
                raise self._refuse("authorize", intent, ["signature_invalid"])
            if self._intents.is_consumed(intent.intent_id):
                raise self._refuse("authorize", intent, ["intent_consumed"])
            self._intents.consume(intent.intent_id)
            result = self._guard.check(intent, now)
            if result.codes:
                raise self._refuse("authorize", intent, result.codes, result)
            # Reads are done: sample the clock again and recheck both deadlines
            # before a success is recorded. A check that started at 15:09:59,
            # or inside the challenge's last second, and finished after the
            # deadline is refused.
            late_now, _ = self._now()
            late = self._late_codes(late_now, challenge.expires_at)
            if late:
                raise self._refuse("authorize", intent, late, result)
            entry = self._append(
                self._entry("authorize", intent, "VERIFIED_NOT_FORWARDED", [], result)
            )
            return AuthorizeResult(
                decision="VERIFIED_NOT_FORWARDED",
                intent_id=intent.intent_id,
                audit_seq=entry["seq"],
                audit_sha256=entry["entry_sha256"],
            )

    def halt(self) -> dict[str, Any]:
        """Mac-initiated halt: sets mac_halt, never clears anything (P-11)."""
        with self._lock:
            latches = self._guard.set_mac_halt()
            result = GuardResult(codes=(), kill=None, latches=tuple(latches))
            self._append(self._entry("halt", None, "HALTED", ["mac_halt"], result))
            return {"contract": CONTRACT, "mac_halt": True}


# --------------------------------------------------------------- the real guard


class KillPort(Protocol):
    def read(self) -> KillState: ...


class AccountPort(Protocol):
    """Fresh holdings, open orders and trade list. Raise on any read failure."""

    def snapshot(self) -> AccountSnapshot: ...


class MarketPort(Protocol):
    """Fresh quote for a stock code, bound to its security master row (P-21)."""

    def quote(self, stock_code: str) -> Quote: ...


class TickReferencePort(Protocol):
    """The band reference price for the tick table (D-09).

    NSE picks a stock's tick from its closing price on the last trading day of
    the PREVIOUS calendar month (or a dated reference the exchange publishes),
    not from yesterday's close. Return that price as a Decimal for the ISIN and
    session date. Raise when it is not known: the guard then answers
    ``tick_reference_unavailable`` and refuses. Bound to real data in 63-05.
    """

    def band_reference(self, isin: str, session_date: date) -> Decimal: ...


class RuleGuard:
    """Guard over the real modules: kill reader, durable state, account and
    market ports, and the pure evaluator.

    Every check reads the kill switch, the state file, the account and the quote
    again; nothing is cached between mint and authorize. A killed switch short
    circuits before any account or market read, so a blocked relay makes no
    broker-side reads. The account read first feeds new fills into the VM ledger
    (which is what clears a stop latch), then looks for an unexplained holding
    (D-04) and latches account_mismatch, persistently, if it finds one.
    """

    def __init__(
        self,
        *,
        limits: Limits,
        kill: KillPort,
        store: StateStore,
        account: AccountPort,
        market: MarketPort,
        tick_reference: TickReferencePort,
        audit: AuditLog,
        tick_table: TickTable | None = None,
    ) -> None:
        self._limits = limits
        self._kill = kill
        self._store = store
        self._account = account
        self._market = market
        self._tick_reference = tick_reference
        self._audit = audit
        self._tick_table = tick_table

    def _state(self):
        return self._store.load_or_init(
            self._limits, audit_has_entries=self._audit.has_entries()
        )

    def check(self, intent: Intent, now: datetime) -> GuardResult:
        with self._store.lock():
            kill = self._kill.read()
            state = self._state()
            if not kill.enabled:
                return GuardResult(
                    codes=("kill_switch",), kill=kill.label, latches=state.latch_names()
                )
            try:
                snapshot = self._account.snapshot()
            except Exception:
                raise refusal("account_read_failed") from None
            changed = apply_trades(state, snapshot.trades)
            if not state.account_mismatch and detect_mismatch(state, snapshot.holdings):
                state.account_mismatch = True
                changed = True
            if changed:
                self._store.save(state)
            try:
                quote: Quote | None = self._market.quote(intent.stock_code)
            except Exception:
                quote = None
            reference = self._band_reference(quote)
            codes = evaluate(
                self._limits,
                state.flags(),
                snapshot,
                quote,
                to_ist(now),
                intent,
                kill_enabled=kill.enabled,
                tick_reference=reference,
                tick_table=self._tick_table,
            )
            return GuardResult(codes=codes, kill=kill.label, latches=state.latch_names())

    def _band_reference(self, quote: Quote | None) -> Decimal | None:
        """Fresh band reference for this quote, or None (fail closed) on any problem."""
        if quote is None:
            return None
        try:
            value = self._tick_reference.band_reference(quote.isin, quote.session_date)
        except Exception:
            return None
        if not isinstance(value, Decimal) or not value.is_finite() or not value > 0:
            return None
        return value

    def set_mac_halt(self) -> tuple[str, ...]:
        with self._store.lock():
            state = self._state()
            state.mac_halt = True
            self._store.save(state)
            return state.latch_names()
