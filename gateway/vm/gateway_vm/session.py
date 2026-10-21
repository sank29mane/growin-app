"""The relay's one Breeze session: login handshake, deadline, relay token.

State lives in memory only (OD-17). A restart starts at NONE. Every way the
session can be missing, stale, revoked or wrong-account fails closed with a
typed RelayFailure, and the lock is never held while a network call is in
flight: copy what is needed, release, call, re-acquire and apply, re-checking
the attempt or generation the decision was based on.

Relay token (OD-16): a 256-bit value minted at arm, returned once and held
only as a sha256 digest. Every comparison goes through hmac.compare_digest.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from enum import Enum
from typing import Protocol
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from .breeze_client import BreezeClient, BreezeError
from .contract import (
    ARM_WINDOW_S,
    EGRESS_MAX_AGE_S,
    ERRORS,
    EVENT_REASON_FOR_CODE,
    LOGIN_URL_BASE,
)
from .egress import EgressChecker, EgressResult
from .events import EventLog
from .transport import Clock

IST = ZoneInfo("Asia/Kolkata")
_EMPTY_DIGEST = hashlib.sha256(b"").digest()


@dataclass(frozen=True)
class Credentials:
    app_key: str = field(repr=False)
    secret_key: str = field(repr=False)


class SessionState(str, Enum):
    NONE = "none"
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED_EGRESS = "suspended_egress"


def session_deadline(login_at_utc: datetime) -> datetime:
    """min(login + 24 h, the next midnight in Asia/Kolkata), tz-aware IST."""
    if login_at_utc.tzinfo is None:
        raise ValueError("session_deadline needs a tz-aware datetime")
    login_ist = login_at_utc.astimezone(IST)
    next_midnight = datetime.combine(
        login_ist.date() + timedelta(days=1), dtime(0, 0), tzinfo=IST
    )
    return min(login_ist + timedelta(hours=24), next_midnight)


# ----------------------------------------------------------------- errors


class BudgetExceeded(Exception):
    def __init__(self, reason: str, retry_after_s: int) -> None:
        self.reason = reason
        self.retry_after_s = retry_after_s
        super().__init__(reason)


class BudgetUnavailable(Exception):
    def __init__(self, reason: str = "counter_unreadable") -> None:
        self.reason = reason
        super().__init__(reason)


class BudgetGate(Protocol):
    def charge(self, kind: str) -> None: ...

    def snapshot(self) -> dict: ...


class RelayFailure(Exception):
    """A contract error (C6). The text is the code only."""

    def __init__(
        self,
        code: str,
        *,
        reason: str | None = None,
        breeze_status: int | None = None,
        retryable: bool | None = None,
        retry_after_s: int | None = None,
        fields: list[str] | None = None,
    ) -> None:
        self.code = code
        self.http = ERRORS[code][0]
        self.reason = reason
        self.breeze_status = breeze_status
        self.retryable = ERRORS[code][1] if retryable is None else retryable
        self.retry_after_s = retry_after_s
        self.fields = fields
        super().__init__(code)

    @property
    def event_reason(self) -> str | None:
        return self.reason if self.reason is not None else EVENT_REASON_FOR_CODE.get(self.code)

    @property
    def event_result(self) -> str:
        return "error" if self.code in ("UPSTREAM_ERROR", "UPSTREAM_TIMEOUT", "INTERNAL") else "rejected"


def payload_failure(
    error_class: str | None,
    *,
    breeze_status: int | None = None,
    http_status: int | None = None,
    login: bool = False,
) -> RelayFailure:
    """Map a non-ok Breeze verdict to its C6 error. No state change here."""
    cls = error_class or "other"
    if cls == "invalid_session":
        if login:  # a rejected code at login is an upstream error, not a dead session
            return RelayFailure("UPSTREAM_ERROR", reason="invalid_session", breeze_status=breeze_status)
        return RelayFailure("SESSION_EXPIRED", reason="invalid_session", breeze_status=breeze_status)
    if cls == "resource_not_available":
        if login:
            return RelayFailure("UPSTREAM_ERROR", reason="other", breeze_status=breeze_status)
        return RelayFailure(
            "SESSION_EXPIRED", reason="resource_not_available", breeze_status=breeze_status
        )
    if cls == "ip_mismatch":
        return RelayFailure("EGRESS_MISMATCH", reason="ip_mismatch", breeze_status=breeze_status)
    if cls == "public_key_missing":
        return RelayFailure("UPSTREAM_ERROR", reason="public_key_missing", breeze_status=breeze_status)
    if cls == "no_data":
        return RelayFailure(
            "UPSTREAM_ERROR", reason="no_data", breeze_status=breeze_status, retryable=True
        )
    if http_status not in (None, 200):
        return RelayFailure("UPSTREAM_ERROR", reason="upstream_http", breeze_status=breeze_status)
    if breeze_status is None:
        return RelayFailure("UPSTREAM_ERROR", reason="upstream_shape")
    return RelayFailure("UPSTREAM_ERROR", reason="other", breeze_status=breeze_status)


def breeze_exception_failure(exc: BreezeError, *, login: bool = False) -> RelayFailure:
    if exc.code == "UPSTREAM_TIMEOUT":
        return RelayFailure("UPSTREAM_TIMEOUT", reason="upstream_timeout")
    if exc.code == "UPSTREAM_UNREACHABLE":
        return RelayFailure("UPSTREAM_TIMEOUT", reason="upstream_unreachable")
    if exc.code == "UPSTREAM_SHAPE":
        return RelayFailure("UPSTREAM_ERROR", reason="upstream_shape")
    return payload_failure(
        exc.error_class,
        breeze_status=exc.breeze_status,
        http_status=exc.http_status,
        login=login,
    )


def charge_budget(budget: BudgetGate, events: EventLog, kind: str = "breeze") -> None:
    """Charge one call before the network, mapping refusals to C6 errors."""
    try:
        budget.charge(kind)
    except BudgetExceeded as exc:
        events.emit("budget_exhausted", result="rejected", reason=exc.reason)
        raise RelayFailure(
            "BUDGET_EXHAUSTED", reason=exc.reason, retry_after_s=exc.retry_after_s
        ) from None
    except BudgetUnavailable:
        raise RelayFailure("BUDGET_UNAVAILABLE", reason="counter_unreadable") from None


def _digest(token: str | None) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest() if token is not None else _EMPTY_DIGEST


def _egress_reason(result: EgressResult) -> str:
    if result.metadata_match is False or result.echo_match is False:
        return "egress_mismatch"
    return "egress_unavailable"


@dataclass(frozen=True)
class Authorization:
    """What one authorize() call decided, read under a single lock hold.

    A data route must use this and nothing else it read earlier: the relay token
    check and the session read cannot be separated by a login or a revoke.
    `failure` is why no Breeze call may proceed (the 503 family), raised by
    require_active() so parameter validation can still run first.
    """

    presented_digest: bytes = field(repr=False)
    breeze_token: str | None = field(repr=False)
    failure: RelayFailure | None
    egress_stale: bool


@dataclass
class _Pending:
    attempt_id: str = field(repr=False)
    token_digest: bytes = field(repr=False)
    mono_deadline: float
    expires_utc: datetime


class SessionManager:
    def __init__(
        self,
        *,
        clock: Clock,
        egress: EgressChecker,
        breeze: BreezeClient,
        credentials: Credentials,
        expected_userid: str,
        events: EventLog,
        budget: BudgetGate,
        arm_window_s: int = ARM_WINDOW_S,
        egress_max_age_s: int = EGRESS_MAX_AGE_S,
    ) -> None:
        self._clock = clock
        self._egress = egress
        self._breeze = breeze
        self._credentials = credentials
        self._expected_userid = expected_userid.encode("utf-8")
        self._events = events
        self._budget = budget
        self._arm_window_s = arm_window_s
        self._egress_max_age_s = egress_max_age_s

        self._lock = threading.Lock()
        self._state = SessionState.NONE
        self._breeze_token: str | None = None
        self._token_digest: bytes | None = None
        self._wall_deadline: datetime | None = None
        self._mono_deadline: float | None = None
        self._dead_reason: str | None = None
        self._pending: _Pending | None = None
        # Advances whenever the login picture changes under an in-flight exchange:
        # an attempt is replaced, the session is dropped (revoke, expiry, session
        # death, egress suspension) or an egress check fails. complete() refuses
        # to install a token if it moved.
        self._generation = 0
        self._last_egress: EgressResult | None = None
        self._last_egress_mono: float | None = None
        # When the newest failed check was recorded. An ok check that started
        # before it can never replace that failure (see _run_egress).
        self._last_failure_mono: float | None = None

    # ------------------------------------------------------------ internals

    def _drop_locked(self, state: SessionState, reason: str | None) -> None:
        """Forget the Breeze token and the relay token, keep the reason."""
        had_session = self._breeze_token is not None
        self._generation += 1
        self._state = state
        self._breeze_token = None
        self._token_digest = None
        self._wall_deadline = None
        self._mono_deadline = None
        self._dead_reason = reason
        if had_session and state is not SessionState.NONE:
            self._events.emit("session_expired", state=state, result="error", reason=reason)

    def _tick_locked(self) -> None:
        """Expire the session when either clock passed its deadline."""
        if self._state is not SessionState.ACTIVE:
            return
        wall_over = (
            self._wall_deadline is None or self._clock.now_utc() >= self._wall_deadline
        )
        mono_over = (
            self._mono_deadline is None or self._clock.monotonic() >= self._mono_deadline
        )
        if wall_over or mono_over or self._breeze_token is None:
            self._drop_locked(SessionState.EXPIRED, "deadline")

    def _inactive_failure_locked(self) -> RelayFailure | None:
        if self._state is SessionState.ACTIVE and self._breeze_token is not None:
            return None
        if self._state is SessionState.EXPIRED:
            return RelayFailure("SESSION_EXPIRED", reason=self._dead_reason or "deadline")
        if self._state is SessionState.SUSPENDED_EGRESS:
            return RelayFailure("EGRESS_MISMATCH", reason=self._dead_reason or "egress_mismatch")
        return RelayFailure("NO_SESSION")

    def _authorize_locked(self, presented_token: str | None) -> None:
        self._authorize_digest_locked(_digest(presented_token), presented_token is None)

    def _authorize_digest_locked(self, presented_digest: bytes, missing: bool = False) -> None:
        if self._token_digest is None:
            return
        same = hmac.compare_digest(presented_digest, self._token_digest)
        if missing or not same:
            raise RelayFailure("UNAUTHORIZED")

    def _superseded_failure_locked(self) -> RelayFailure:
        """Why an exchange that started under an older generation may not install."""
        if self._state is SessionState.SUSPENDED_EGRESS:
            return RelayFailure("EGRESS_MISMATCH", reason=self._dead_reason or "egress_mismatch")
        return RelayFailure("LOGIN_ATTEMPT_INVALID")

    def _egress_stale_locked(self) -> bool:
        """True unless the last check passed and is younger than the limit.

        A failed check counts as stale: it is never a result to ride on, and the
        window between recording it and suspending the session stays closed.
        """
        return (
            self._last_egress is None
            or not self._last_egress.ok
            or self._last_egress_mono is None
            or self._clock.monotonic() - self._last_egress_mono >= self._egress_max_age_s
        )

    def _egress_unusable_failure_locked(self) -> RelayFailure:
        last = self._last_egress
        reason = _egress_reason(last) if last is not None and not last.ok else "egress_unavailable"
        return RelayFailure("EGRESS_MISMATCH", reason=reason)

    def _run_egress(self) -> EgressResult:
        """One network check, outside the lock. Records the result.

        The evidence is stamped with the moment the check started, so a slow
        check cannot look fresher than what it actually observed. A failure
        always replaces what is stored. An ok whose start is not after the
        newest recorded failure is dropped and that failure stays: otherwise a
        slow ok that began before a fast failing check could land after it and
        leave fresh ok evidence for the whole age limit.
        """
        started_mono = self._clock.monotonic()
        result, cached = self._egress.check_reporting_cache(keep_failure_on_ok=True)
        with self._lock:
            superseded = (
                result.ok
                and self._last_failure_mono is not None
                and started_mono <= self._last_failure_mono
            )
            if superseded:
                kept = self._last_egress  # what the gate keeps: the failure, or a newer ok stored since
            else:
                self._last_egress = result
                self._last_egress_mono = started_mono
                if result.ok:
                    self._egress.clear_failure_before(started_mono)  # a dropped ok never gets here
                else:
                    self._last_failure_mono = self._clock.monotonic()
                    self._generation += 1  # an exchange already in flight is no longer trustworthy
                kept = result
        logged = kept if kept is not None else result  # the audit line says what the gate kept
        self._events.emit(
            "egress_check",
            result="ok" if logged.ok else ("rejected" if _egress_reason(logged) == "egress_mismatch" else "error"),
            reason=None if logged.ok else _egress_reason(logged),
            cached=cached,
        )
        return logged

    def _refresh_egress(self) -> EgressResult:
        """Check egress and, on failure, suspend an active session."""
        result = self._run_egress()
        if not result.ok:
            with self._lock:
                if self._state is SessionState.ACTIVE:
                    self._drop_locked(SessionState.SUSPENDED_EGRESS, _egress_reason(result))
        return result

    # --------------------------------------------------------------- public

    def arm(self, presented_token: str | None) -> tuple[str, str, str, datetime]:
        with self._lock:
            self._tick_locked()
            self._authorize_locked(presented_token)
            attempt_id = secrets.token_urlsafe(16)  # 128 bit
            relay_token = secrets.token_urlsafe(32)  # 256 bit
            self._generation += 1  # an exchange of an earlier attempt must not land
            expires = self._clock.now_utc() + timedelta(seconds=self._arm_window_s)
            self._pending = _Pending(
                attempt_id=attempt_id,
                token_digest=_digest(relay_token),
                mono_deadline=self._clock.monotonic() + self._arm_window_s,
                expires_utc=expires,
            )
            login_url = LOGIN_URL_BASE + quote_plus(self._credentials.app_key)
            self._events.emit("login_armed", state=self._state, result="ok")
        return attempt_id, login_url, relay_token, expires

    def complete(self, attempt_id: str, apisession: str, presented_token: str | None) -> datetime:
        try:
            return self._complete(attempt_id, apisession, presented_token)
        except RelayFailure as failure:
            self._events.emit(
                "login_completed",
                state=self.state,
                result=failure.event_result,
                reason=failure.event_reason,
                breeze_status=failure.breeze_status,
            )
            raise

    @property
    def state(self) -> SessionState:
        with self._lock:
            self._tick_locked()
            return self._state

    def _complete(self, attempt_id: str, apisession: str, presented_token: str | None) -> datetime:
        with self._lock:
            self._tick_locked()
            pending = self._pending
            expected = pending.token_digest if pending is not None else _EMPTY_DIGEST
            token_ok = hmac.compare_digest(_digest(presented_token), expected)
            if pending is None:
                raise RelayFailure("LOGIN_ATTEMPT_INVALID")
            if presented_token is None or not token_ok:
                raise RelayFailure("UNAUTHORIZED")  # the attempt stays usable
            id_ok = hmac.compare_digest(attempt_id.encode("utf-8"), pending.attempt_id.encode("utf-8"))
            if not id_ok:
                raise RelayFailure("LOGIN_ATTEMPT_INVALID")
            if self._clock.monotonic() >= pending.mono_deadline:
                self._pending = None
                raise RelayFailure("LOGIN_ATTEMPT_INVALID")
            self._pending = None  # disarm before any Breeze call: single use
            new_digest = pending.token_digest
            generation = self._generation

        egress = self._run_egress()
        if not egress.ok:
            with self._lock:
                self._drop_locked(SessionState.SUSPENDED_EGRESS, _egress_reason(egress))
            raise RelayFailure("EGRESS_MISMATCH", reason=_egress_reason(egress))

        charge_budget(self._budget, self._events)

        try:
            details = self._breeze.customer_details(
                app_key=self._credentials.app_key, api_session=apisession
            )
        except BreezeError as exc:
            failure = breeze_exception_failure(exc, login=True)
            if failure.reason == "public_key_missing":
                self._events.emit("config_alarm", result="error", reason="public_key_missing")
            if failure.code == "EGRESS_MISMATCH":
                with self._lock:
                    self._drop_locked(SessionState.SUSPENDED_EGRESS, "ip_mismatch")
            raise failure from None

        user_ok = hmac.compare_digest(details.userid.encode("utf-8"), self._expected_userid)
        if not user_ok:
            raise RelayFailure("IDENTITY_MISMATCH", reason="identity_mismatch")  # token discarded
        if details.segments.get("Equity") != "Y" or details.segments.get("Trading") != "Y":
            raise RelayFailure("IDENTITY_MISMATCH", reason="segment_disabled")

        with self._lock:
            if self._generation != generation:
                raise self._superseded_failure_locked()  # replaced, revoked or suspended meanwhile
            now = self._clock.now_utc()
            deadline = session_deadline(now)
            self._breeze_token = details.session_token
            self._wall_deadline = deadline
            self._mono_deadline = self._clock.monotonic() + (deadline - now).total_seconds()
            self._token_digest = new_digest
            self._state = SessionState.ACTIVE
            self._dead_reason = None
            self._events.emit("login_completed", state=self._state, result="ok")
            return deadline

    def authorize(self, presented_token: str | None) -> Authorization:
        with self._lock:
            self._tick_locked()
            self._authorize_locked(presented_token)
            failure = self._inactive_failure_locked()
            return Authorization(
                presented_digest=_digest(presented_token),
                breeze_token=self._breeze_token if failure is None else None,
                failure=failure,
                egress_stale=failure is None and self._egress_stale_locked(),
            )

    def _revalidate_locked(self, auth: Authorization) -> str:
        """The Breeze token, if the session `auth` was issued for is still live.

        Caller holds the lock. Deadline, relay token, then session state, so a
        deadline, a revoke, a re-login, a session-dead error or an egress
        suspension that landed since authorize() all refuse here.
        """
        self._tick_locked()
        self._authorize_digest_locked(auth.presented_digest)
        failure = self._inactive_failure_locked()
        if failure is not None:
            raise failure
        if self._breeze_token is None:
            raise RelayFailure("NO_SESSION")  # fail closed, not an assert that -O strips
        return self._breeze_token

    def require_active(self, auth: Authorization) -> str:
        """The Breeze token for this request, only if the session is live right now.

        Always re-read under the lock, whatever authorize() saw: the deadline
        is ticked, the relay token must still be the current one and the
        session must still be active. If egress is stale at that moment the
        refresh runs outside the lock and all of it is repeated afterwards, so
        a revoke, expiry, session-dead error, suspension or re-login between
        authorize() and the Breeze call never yields a token.
        """
        if auth.failure is not None:
            raise auth.failure
        with self._lock:
            token = self._revalidate_locked(auth)
            stale = self._egress_stale_locked()
        if not stale:
            return token
        self._refresh_egress()
        with self._lock:
            token = self._revalidate_locked(auth)
            if self._egress_stale_locked():  # failed, or aged out during the refresh: no retry
                raise self._egress_unusable_failure_locked()
            return token

    def note_breeze_error(
        self,
        error_class: str | None,
        *,
        breeze_status: int | None = None,
        http_status: int | None = None,
    ) -> RelayFailure:
        with self._lock:
            if error_class == "invalid_session":
                self._drop_locked(SessionState.EXPIRED, "invalid_session")
            elif error_class == "resource_not_available":
                self._drop_locked(SessionState.EXPIRED, "resource_not_available")
            elif error_class == "ip_mismatch":
                self._drop_locked(SessionState.SUSPENDED_EGRESS, "ip_mismatch")
            elif error_class == "public_key_missing":
                self._drop_locked(SessionState.NONE, None)
                self._events.emit("config_alarm", result="error", reason="public_key_missing")
        return payload_failure(error_class, breeze_status=breeze_status, http_status=http_status)

    def revoke(self, presented_token: str | None) -> None:
        with self._lock:
            self._tick_locked()
            self._authorize_locked(presented_token)
            self._pending = None
            self._drop_locked(SessionState.NONE, None)
            self._events.emit("session_revoked", state=self._state, result="ok")

    def snapshot(self, *, refresh_egress_if_stale: bool) -> dict:
        if refresh_egress_if_stale:
            with self._lock:
                self._tick_locked()
                due = self._state is SessionState.ACTIVE and self._egress_stale_locked()
            if due:  # only an active session pays for a network check on a health poll
                self._refresh_egress()
        with self._lock:
            self._tick_locked()
            pending = self._pending
            login_pending = pending is not None and self._clock.monotonic() < pending.mono_deadline
            egress = self._last_egress
            return {
                "state": self._state.value,
                "login_pending": login_pending,
                "expires_at_ist": (
                    self._wall_deadline.isoformat() if self._wall_deadline is not None else None
                ),
                "egress": {
                    "ok": egress.ok if egress is not None else None,
                    "checked_at_utc": (
                        egress.checked_at_utc.astimezone(timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z")
                        if egress is not None
                        else None
                    ),
                },
            }

    def require_egress(self, presented_token: str | None) -> None:
        """Gate a route that needs good egress evidence but no Breeze session.

        Same pattern as require_active: under the lock the bearer (when a session
        is bound) must be current and the last egress result must be ok and
        fresh. If it is not, one check runs outside the lock and all of it is
        repeated, so a result that aged out or failed during the refresh, a
        failed concurrent check or a bearer replaced meanwhile never passes.
        """
        digest = _digest(presented_token)
        missing = presented_token is None
        with self._lock:
            self._tick_locked()
            self._authorize_digest_locked(digest, missing)
            stale = self._egress_stale_locked()
        if not stale:
            return
        self._refresh_egress()
        with self._lock:
            self._tick_locked()
            self._authorize_digest_locked(digest, missing)
            if self._egress_stale_locked():
                raise self._egress_unusable_failure_locked()
