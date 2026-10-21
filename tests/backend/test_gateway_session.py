"""SessionManager: the IST deadline, the login attempt, egress, identity, the
relay token and every fail-closed path (Phase 61-05, GATE-02, GATE-05).

Built on the harness of test_gateway_relay_app.py: the real app, session,
client and egress checker over a FakeTransport. No network.
"""

from __future__ import annotations

import hmac
import threading
from datetime import datetime, timedelta, timezone

import pytest
from test_gateway_relay_app import (
    APP_KEY,
    BARS_BODY,
    BARS_QUERY,
    BREEZE_TOKEN,
    CODE,
    CONTRACT_HDR,
    EXPECTED_IP,
    OTHER_IP,
    T0,
    Stack,
    USERID,
    auth,
    breeze_body,
    customer_body,
)

from gateway_vm import contract  # noqa: E402
from gateway_vm.session import (  # noqa: E402
    IST,
    BudgetExceeded,
    BudgetUnavailable,
    RelayFailure,
    SessionState,
    session_deadline,
)
from gateway_vm.transport import HttpResponse, TransportError  # noqa: E402

UTC = timezone.utc


def code_of(resp) -> str:
    return resp.json()["error"]["code"]


def reason_of(resp) -> str | None:
    return resp.json()["error"]["reason"]


def direct_login(stack: Stack) -> str:
    attempt_id, _url, token, _exp = stack.session.arm(None)
    stack.session.complete(attempt_id, CODE, token)
    return token


# ------------------------------------------------------------- deadline


@pytest.mark.parametrize(
    "login_utc, expected_ist",
    [
        (datetime(2026, 10, 2, 18, 29, tzinfo=UTC), datetime(2026, 10, 3, 0, 0, tzinfo=IST)),
        (datetime(2026, 10, 2, 18, 31, tzinfo=UTC), datetime(2026, 10, 4, 0, 0, tzinfo=IST)),
        (datetime(2026, 10, 2, 3, 30, tzinfo=UTC), datetime(2026, 10, 3, 0, 0, tzinfo=IST)),
        (datetime(2026, 10, 2, 18, 30, tzinfo=UTC), datetime(2026, 10, 4, 0, 0, tzinfo=IST)),
    ],
)
def test_session_deadline_is_min_of_24h_and_next_ist_midnight(login_utc, expected_ist):
    got = session_deadline(login_utc)
    assert got == expected_ist
    assert got.utcoffset() == timedelta(hours=5, minutes=30)
    assert str(got.tzinfo) == "Asia/Kolkata"
    assert got - login_utc <= timedelta(hours=24)


def test_deadline_needs_tz_aware_input():
    with pytest.raises(ValueError):
        session_deadline(datetime(2026, 10, 2, 12, 0))


@pytest.mark.parametrize("which", ["wall", "mono"])
def test_either_clock_past_the_deadline_expires_the_session(which):
    stack = Stack()
    token = direct_login(stack)
    assert stack.session.require_active(stack.session.authorize(token)) == BREEZE_TOKEN
    if which == "wall":
        stack.clock.now = stack.clock.now.replace(hour=18, minute=30)  # IST midnight
    else:
        stack.clock.mono += 14 * 3600 + 1
    for _ in range(2):  # second call must still fail closed, one event only
        with pytest.raises(RelayFailure) as err:
            stack.session.require_active(stack.session.authorize(token))
        assert (err.value.code, err.value.reason, err.value.http) == ("SESSION_EXPIRED", "deadline", 503)
    expired = stack.events_named("session_expired")
    assert len(expired) == 1 and expired[0]["reason"] == "deadline"
    assert stack.session.state is SessionState.EXPIRED
    assert stack.session._breeze_token is None
    # the dead session's relay token is gone too: no 401, the 503 family applies
    resp = stack.bars(token)
    assert resp.status_code == 503 and code_of(resp) == "SESSION_EXPIRED"
    assert len(stack.transport.bars_requests()) == 0


def test_the_deadline_is_checked_on_every_route_path_not_only_require_active():
    stack = Stack()
    token = stack.login()
    stack.clock.advance(15 * 3600)
    health = stack.client.get("/v1/health", headers=CONTRACT_HDR).json()
    assert health["state"] == "expired"
    assert stack.revoke(token).status_code == 200  # token already dead, revoke is a no-op 200


def test_restart_starts_with_no_session():
    stack = Stack()
    stack.login()
    fresh = Stack()  # a new process: new manager, nothing carried over
    resp = fresh.bars(None)
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert fresh.transport.requests == []
    assert fresh.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] == "none"


# ----------------------------------------------------------- relay token


def test_only_digests_are_stored_for_the_relay_token():
    stack = Stack()
    attempt_id, _url, token, _exp = stack.session.arm(None)
    stack.session.complete(attempt_id, CODE, token)

    def walk(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, (str, bytes)):
            assert token.encode() not in (value if isinstance(value, bytes) else value.encode())
        elif hasattr(value, "__dict__"):
            for item in vars(value).values():
                walk(item, depth + 1)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)

    walk(stack.session)
    assert isinstance(stack.session._token_digest, bytes) and len(stack.session._token_digest) == 32


def test_authorize_is_constant_time_and_never_skips_the_compare(monkeypatch):
    stack = Stack()
    token = direct_login(stack)
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr("gateway_vm.session.hmac.compare_digest", spy)
    stack.session.authorize(token)
    assert len(calls) == 1
    for bad in (None, "x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B")):
        with pytest.raises(RelayFailure) as err:
            stack.session.authorize(bad)
        assert err.value.code == "UNAUTHORIZED"
    assert len(calls) == 4  # a missing token is compared too
    assert all(a == b == 32 for a, b in calls)  # digests, equal length


def test_wrong_bearer_is_401_on_every_route_while_a_session_is_bound():
    stack = Stack()
    token = stack.login()
    wrong = "B" * 43
    for bad in (None, wrong):
        assert code_of(stack.bars(bad)) == "UNAUTHORIZED"
        assert code_of(stack.revoke(bad)) == "UNAUTHORIZED"
        assert code_of(stack.arm(bad)) == "UNAUTHORIZED"  # arm needs the token while active
    for malformed in ("short", "Bearer", "Bearer " + "a" * 44, "Basic " + "a" * 43):
        resp = stack.client.get(BARS_QUERY, headers={**CONTRACT_HDR, "Authorization": malformed})
        assert resp.status_code == 401
    assert stack.session.state is SessionState.ACTIVE  # none of that revoked anything
    assert stack.bars(token).status_code == 200


def test_with_nothing_bound_the_503_family_applies_not_401():
    stack = Stack()
    resp = stack.bars("C" * 43)
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert stack.revoke("C" * 43).status_code == 200  # revoke with nothing bound is a no-op 200
    assert stack.arm("C" * 43).status_code == 200  # arm with no session needs no token


def test_failed_re_login_keeps_the_old_session_and_token():
    stack = Stack()
    old = stack.login()
    armed = stack.arm(old).json()
    new = armed["relay_token"]
    stack.transport.customer = HttpResponse(200, {}, customer_body(userid="OTHERUSER9"))
    refused = stack.complete(armed["attempt_id"], new)
    assert refused.status_code == 403 and reason_of(refused) == "identity_mismatch"
    assert stack.bars(old).status_code == 200  # old session and token intact
    assert code_of(stack.bars(new)) == "UNAUTHORIZED"  # the new token never became a session token


def test_successful_re_login_replaces_the_digest_only_after_the_exchange():
    stack = Stack()
    old = stack.login()
    armed = stack.arm(old).json()
    new = armed["relay_token"]
    # between arm and complete the old token is still the session token
    assert stack.bars(old).status_code == 200
    assert stack.complete(armed["attempt_id"], new).status_code == 200
    assert code_of(stack.bars(old)) == "UNAUTHORIZED"
    assert stack.bars(new).status_code == 200


def test_complete_with_the_old_session_token_is_not_the_arms_token():
    stack = Stack()
    old = stack.login()
    armed = stack.arm(old).json()
    refused = stack.complete(armed["attempt_id"], old)  # session token, not the arm's token
    assert refused.status_code == 401
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200


@pytest.mark.parametrize("what", ["revoke", "deadline", "invalid_session", "ip_mismatch"])
def test_token_dies_with_the_session(what):
    stack = Stack()
    token = stack.login()
    if what == "revoke":
        stack.revoke(token)
    elif what == "deadline":
        stack.clock.advance(15 * 3600)
    elif what == "invalid_session":
        stack.transport.bars = HttpResponse(200, {}, breeze_body(None, 500, "Invalid session."))
        assert stack.bars(token).status_code == 503
    else:
        stack.transport.bars = HttpResponse(
            200, {}, breeze_body(None, 500, "IP address used does not match the registered IP")
        )
        assert stack.bars(token).status_code == 503
    resp = stack.bars(token)  # the deadline is noticed on the next call
    assert resp.status_code == 503 and code_of(resp) != "UNAUTHORIZED"
    assert stack.session._token_digest is None


def test_twenty_threads_mixing_bearers_never_get_200_with_a_wrong_one():
    stack = Stack()
    token = stack.login()
    results: list[tuple[bool, int]] = []
    guard = threading.Lock()

    def worker(i: int) -> None:
        client = stack.new_client()
        for j in range(5):
            right = (i + j) % 2 == 0
            resp = stack.bars(token if right else "D" * 43, client=client)
            with guard:
                results.append((right, resp.status_code))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 100
    assert all(code == 401 for right, code in results if not right)
    assert all(code == 200 for right, code in results if right)


# --------------------------------------------------------------- attempt


def test_wrong_attempt_id_is_409_and_leaves_the_real_attempt_usable():
    stack = Stack()
    armed = stack.arm().json()
    wrong = stack.complete("x" * 22, armed["relay_token"])
    assert wrong.status_code == 409 and code_of(wrong) == "LOGIN_ATTEMPT_INVALID"
    assert stack.transport.requests == []
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200


def test_attempt_window_is_120_seconds():
    stack = Stack()
    armed = stack.arm().json()
    stack.clock.advance(119)
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["login_pending"] is True
    stack.clock.advance(2)  # 121 s
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["login_pending"] is False
    late = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert late.status_code == 409 and code_of(late) == "LOGIN_ATTEMPT_INVALID"
    assert stack.transport.requests == [] or stack.transport.breeze_requests() == []


def test_attempt_is_single_use():
    stack = Stack()
    armed = stack.arm().json()
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200
    again = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert again.status_code == 409


def test_a_second_arm_kills_the_first_id():
    stack = Stack()
    first = stack.arm().json()
    second = stack.arm().json()
    # the first token no longer matches the pending digest (401), and the first id is dead (409)
    assert stack.complete(first["attempt_id"], first["relay_token"]).status_code == 401
    assert stack.complete(first["attempt_id"], second["relay_token"]).status_code == 409
    assert stack.transport.breeze_requests() == []
    assert stack.complete(second["attempt_id"], second["relay_token"]).status_code == 200


def test_apisession_failing_the_filter_is_422_and_does_not_consume_the_attempt():
    stack = Stack()
    armed = stack.arm().json()
    marker = "SUPERSECRETMARKER-with-bad-chars!"
    for bad in (marker, "12345", "x" * 65, "12 345678", ""):
        resp = stack.complete(armed["attempt_id"], armed["relay_token"], code=bad)
        assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
        assert marker not in resp.text and bad not in resp.text.replace('"fields"', "") or bad == ""
        assert resp.json()["error"]["fields"] == ["apisession"]
    assert stack.transport.requests == []
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200


def test_attempt_is_disarmed_before_customer_details_is_called():
    stack = Stack()
    armed = stack.arm().json()
    stack.transport.customer = TransportError("timeout", "api.icicidirect.com")
    first = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert first.status_code == 504 and code_of(first) == "UPSTREAM_TIMEOUT"
    stack.transport.customer = HttpResponse(200, {}, customer_body())
    retry = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert retry.status_code == 409  # the attempt died with the first try


def test_attempt_id_and_token_checks_use_compare_digest(monkeypatch):
    stack = Stack()
    armed = stack.arm().json()
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        "gateway_vm.session.hmac.compare_digest", lambda a, b: calls.append(1) or real(a, b)
    )
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200
    assert len(calls) == 3  # relay token, attempt id, user id


# ---------------------------------------------------------------- egress


@pytest.mark.parametrize(
    "tweak",
    [
        lambda t: setattr(t, "metadata_ip", OTHER_IP),
        lambda t: setattr(t, "echo_ip", OTHER_IP),
        lambda t: setattr(t, "echo_ip", TransportError("timeout", "api.ipify.org")),
    ],
    ids=["metadata_mismatch", "echo_mismatch", "echo_unreachable"],
)
def test_egress_failure_blocks_complete_with_zero_breeze_requests(tweak):
    stack = Stack()
    tweak(stack.transport)
    armed = stack.arm().json()
    resp = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert reason_of(resp) in ("egress_mismatch", "egress_unavailable")
    assert stack.transport.breeze_requests() == []
    assert stack.budget.kinds == []  # not even charged
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] == "suspended_egress"
    assert OTHER_IP not in resp.text and EXPECTED_IP not in resp.text


def test_egress_mismatch_reasons_are_distinguished():
    stack = Stack()
    stack.transport.echo_ip = TransportError("timeout", "api.ipify.org")
    armed = stack.arm().json()
    assert reason_of(stack.complete(armed["attempt_id"], armed["relay_token"])) == "egress_unavailable"
    stack.transport.echo_ip = OTHER_IP
    armed = stack.arm().json()
    assert reason_of(stack.complete(armed["attempt_id"], armed["relay_token"])) == "egress_mismatch"


def test_stale_egress_is_rechecked_and_a_mismatch_suspends_the_session():
    stack = Stack()
    token = stack.login()
    before = len(stack.transport.requests)
    stack.clock.advance(200)
    assert stack.bars(token).status_code == 200
    metadata_calls = [r for r in stack.transport.requests if "metadata" in r[1]]
    assert len(metadata_calls) == 1  # still fresh at 200 s: no new check
    stack.clock.advance(101)  # 301 s since the login check
    stack.transport.echo_ip = OTHER_IP
    resp = stack.bars(token)
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert reason_of(resp) == "egress_mismatch"
    assert len(stack.transport.bars_requests()) == 1  # the second bars call never left
    assert stack.session.state is SessionState.SUSPENDED_EGRESS
    assert stack.session._breeze_token is None and stack.session._token_digest is None
    again = stack.bars(token)
    assert again.status_code == 503 and code_of(again) == "EGRESS_MISMATCH"
    assert before > 0


def test_egress_fresh_refreshes_when_older_than_the_limit():
    stack = Stack()
    assert stack.session.egress_fresh() is True
    n = len(stack.transport.requests)
    assert stack.session.egress_fresh() is True
    assert len(stack.transport.requests) == n
    stack.clock.advance(301)
    stack.transport.metadata_ip = OTHER_IP
    assert stack.session.egress_fresh() is False


# -------------------------------------------------------------- identity


def test_a_different_user_id_is_403_identity_mismatch_and_the_token_is_discarded():
    stack = Stack()
    stack.transport.customer = HttpResponse(200, {}, customer_body(userid="SOMEONEELSE"))
    armed = stack.arm().json()
    resp = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert resp.status_code == 403 and code_of(resp) == "IDENTITY_MISMATCH"
    assert reason_of(resp) == "identity_mismatch"
    assert "SOMEONEELSE" not in resp.text and USERID not in resp.text
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] == "none"
    after = stack.bars(None)
    assert after.status_code == 503 and code_of(after) == "NO_SESSION"
    assert stack.session._breeze_token is None


@pytest.mark.parametrize("kwargs", [{"equity": "N"}, {"trading": "N"}])
def test_equity_or_trading_not_enabled_is_403_segment_disabled(kwargs):
    stack = Stack()
    stack.transport.customer = HttpResponse(200, {}, customer_body(**kwargs))
    armed = stack.arm().json()
    resp = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert resp.status_code == 403 and reason_of(resp) == "segment_disabled"
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] == "none"
    assert code_of(stack.bars(None)) == "NO_SESSION"
    failed = [e for e in stack.events_named("login_completed") if e["result"] != "ok"]
    assert failed and failed[-1]["reason"] == "segment_disabled"


@pytest.mark.parametrize(
    "response, http, code, reason",
    [
        (HttpResponse(200, {}, breeze_body(None, 500, "Invalid session.")), 502, "UPSTREAM_ERROR", "invalid_session"),
        (HttpResponse(200, {}, breeze_body(None, 500, "Public Key does not exist.")), 502, "UPSTREAM_ERROR", "public_key_missing"),
        (HttpResponse(200, {}, b"not json"), 502, "UPSTREAM_ERROR", "upstream_shape"),
        (HttpResponse(200, {}, breeze_body(None, 500, "IP address used does not match x")), 503, "EGRESS_MISMATCH", "ip_mismatch"),
        (TransportError("timeout", "h"), 504, "UPSTREAM_TIMEOUT", "upstream_timeout"),
        (TransportError("connect", "h"), 504, "UPSTREAM_TIMEOUT", "upstream_unreachable"),
    ],
)
def test_breeze_errors_on_the_login_exchange_map_to_the_c6_rows(response, http, code, reason):
    stack = Stack()
    stack.transport.customer = response
    armed = stack.arm().json()
    resp = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (http, code, reason)
    assert stack.session.state is not SessionState.ACTIVE
    if reason == "public_key_missing":
        assert stack.events_named("config_alarm")


# ----------------------------------------------------- Breeze errors on bars


def test_session_dead_errors_on_bars_fail_closed_and_stay_closed():
    for text, reason in (("Invalid session.", "invalid_session"), ("Resource not available.", "resource_not_available")):
        stack = Stack()
        token = stack.login()
        stack.transport.bars = HttpResponse(200, {}, breeze_body(None, 500, text))
        first = stack.bars(token)
        assert (first.status_code, code_of(first), reason_of(first)) == (503, "SESSION_EXPIRED", reason)
        n = len(stack.transport.bars_requests())
        later = stack.bars(token)
        assert later.status_code == 503 and code_of(later) == "SESSION_EXPIRED"
        assert len(stack.transport.bars_requests()) == n  # no Breeze call after the session died
        assert text not in first.text


def test_ip_mismatch_error_on_bars_is_egress_mismatch():
    stack = Stack()
    token = stack.login()
    stack.transport.bars = HttpResponse(
        200, {}, breeze_body(None, 500, "IP address used does not match the registered IP")
    )
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (503, "EGRESS_MISMATCH", "ip_mismatch")
    assert stack.session.state is SessionState.SUSPENDED_EGRESS


def test_http_401_with_status_5_is_upstream_http():
    stack = Stack()
    token = stack.login()
    stack.transport.bars = HttpResponse(401, {}, breeze_body(None, 5, "Unauthorized"))
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (502, "UPSTREAM_ERROR", "upstream_http")
    assert resp.json()["error"]["breeze_status"] == 5
    assert stack.session.state is SessionState.ACTIVE  # not a session-dead error


def test_empty_success_is_200_flagged_empty():
    stack = Stack()
    token = stack.login()
    for empty in ([], {}):
        stack.transport.bars = HttpResponse(200, {}, breeze_body(empty))
        resp = stack.bars(token)
        assert resp.status_code == 200 and resp.headers[contract.HDR_EMPTY] == "true"


def test_no_data_error_is_502_retryable():
    stack = Stack()
    token = stack.login()
    stack.transport.bars = HttpResponse(200, {}, breeze_body(None, 500, "No Data Found."))
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (502, "UPSTREAM_ERROR", "no_data")
    err = resp.json()["error"]
    assert err["retryable"] is True and err["breeze_status"] == 500
    assert stack.session.state is SessionState.ACTIVE
    assert stack.session._breeze_token is not None


@pytest.mark.parametrize(
    "response, http, code, reason",
    [
        (HttpResponse(200, {}, b"<html>"), 502, "UPSTREAM_ERROR", "upstream_shape"),
        (HttpResponse(500, {}, b"<html>"), 502, "UPSTREAM_ERROR", "upstream_http"),
        (HttpResponse(200, {}, breeze_body(None, 500, "boom")), 502, "UPSTREAM_ERROR", "other"),
        (TransportError("timeout", "h"), 504, "UPSTREAM_TIMEOUT", "upstream_timeout"),
        (TransportError("connect", "h"), 504, "UPSTREAM_TIMEOUT", "upstream_unreachable"),
        (TransportError("tls", "h"), 504, "UPSTREAM_TIMEOUT", "upstream_unreachable"),
    ],
)
def test_other_upstream_failures_map_to_the_c6_rows(response, http, code, reason):
    stack = Stack()
    token = stack.login()
    stack.transport.bars = response
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (http, code, reason)
    assert "boom" not in resp.text
    assert stack.session.state is SessionState.ACTIVE


def test_public_key_missing_on_a_data_route_drops_to_none_with_an_alarm():
    stack = Stack()
    token = stack.login()
    stack.transport.bars = HttpResponse(200, {}, breeze_body(None, 500, "Public Key does not exist."))
    resp = stack.bars(token)
    assert (resp.status_code, reason_of(resp)) == (502, "public_key_missing")
    assert stack.session.state is SessionState.NONE
    assert stack.events_named("config_alarm")
    assert code_of(stack.bars(token)) == "NO_SESSION"


# ---------------------------------------------------------------- budget


def test_budget_exceeded_blocks_bars_before_the_network():
    stack = Stack()
    token = stack.login()
    n = len(stack.transport.requests)
    stack.budget.raises = BudgetExceeded("budget_minute", 12)
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (429, "BUDGET_EXHAUSTED", "budget_minute")
    err = resp.json()["error"]
    assert err["retry_after_s"] == 12 and err["retryable"] is True
    assert len(stack.transport.requests) == n
    assert stack.events_named("budget_exhausted")


def test_budget_unavailable_blocks_bars_and_the_login_exchange():
    stack = Stack()
    token = stack.login()
    n = len(stack.transport.requests)
    stack.budget.raises = BudgetUnavailable("counter_unreadable")
    resp = stack.bars(token)
    assert (resp.status_code, code_of(resp), reason_of(resp)) == (503, "BUDGET_UNAVAILABLE", "counter_unreadable")
    assert len(stack.transport.requests) == n

    fresh = Stack()
    fresh.budget.raises = BudgetUnavailable("counter_unreadable")
    armed = fresh.arm().json()
    done = fresh.complete(armed["attempt_id"], armed["relay_token"])
    assert (done.status_code, code_of(done)) == (503, "BUDGET_UNAVAILABLE")
    assert fresh.transport.breeze_requests() == []  # no customerdetails request left


def test_budget_exceeded_blocks_the_login_exchange():
    stack = Stack()
    stack.budget.raises = BudgetExceeded("budget_day", 3600)
    armed = stack.arm().json()
    done = stack.complete(armed["attempt_id"], armed["relay_token"])
    assert (done.status_code, code_of(done), reason_of(done)) == (429, "BUDGET_EXHAUSTED", "budget_day")
    assert stack.transport.breeze_requests() == []


def test_the_budget_is_charged_once_per_breeze_call():
    stack = Stack()
    token = stack.login()
    stack.bars(token)
    stack.bars(token)
    assert stack.budget.kinds == ["breeze"] * 3  # the login exchange plus two bars calls


# ----------------------------------------------------------- concurrency


def test_twenty_threads_on_bars_leave_the_state_consistent():
    stack = Stack()
    token = stack.login()
    outcomes: list[int] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def worker() -> None:
        try:
            client = stack.new_client()
            resp = stack.bars(token, client=client)
            with guard:
                outcomes.append(resp.status_code)
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert outcomes == [200] * 20
    assert stack.session.state is SessionState.ACTIVE
    assert stack.session._breeze_token == BREEZE_TOKEN
    assert len(stack.transport.bars_requests()) == 20


def test_threads_racing_a_session_death_all_get_a_c6_error_or_200():
    stack = Stack()
    token = stack.login()
    stack.transport.bars = HttpResponse(200, {}, breeze_body(None, 500, "Invalid session."))
    statuses: list[int] = []
    guard = threading.Lock()

    def worker() -> None:
        resp = stack.bars(token, client=stack.new_client())
        with guard:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(statuses) == {503}
    assert stack.session.state is SessionState.EXPIRED
    assert stack.session._breeze_token is None
    assert len(stack.events_named("session_expired")) == 1


# -------------------------------------------- deterministic interleavings
#
# No sleeps: a request is parked inside a fake network call (or between two
# session calls) with an Event pair, the test does the competing step on the
# main thread, then releases it.

WAIT_S = 10


class Parked:
    """One request running on its own thread until `release()`."""

    def __init__(self, fn) -> None:
        self.result = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def _run(self, fn) -> None:
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            self.error = exc

    def start(self) -> "Parked":
        self._thread.start()
        return self

    def join(self):
        self._thread.join(WAIT_S)
        assert not self._thread.is_alive(), "parked request never finished"
        assert self.error is None, repr(self.error)
        return self.result


def park_next_request(stack: Stack, match):
    """Park the next transport request whose URL satisfies `match`, once.

    Returns (entered, release): `entered` is set when a request is parked,
    the parked request resumes when `release` is set.
    """
    entered, release = threading.Event(), threading.Event()
    real = stack.transport.request
    armed = [True]

    def gated(method, url, **kwargs):
        if armed[0] and match(url):
            armed[0] = False
            entered.set()
            assert release.wait(WAIT_S), "test never released the parked request"
        return real(method, url, **kwargs)

    stack.transport.request = gated  # type: ignore[method-assign]
    return entered, release


def is_customerdetails(url: str) -> bool:
    return url.endswith("/customerdetails")


def is_echo(url: str) -> bool:
    return "api.ipify.org" in url


def start_complete(stack: Stack, armed: dict) -> Parked:
    client = stack.new_client()
    return Parked(lambda: stack.complete(armed["attempt_id"], armed["relay_token"], client=client)).start()


def test_a_tokenless_bars_request_that_passed_authorize_cannot_ride_a_login_that_lands_meanwhile():
    """Codex P1: authorize() saw no session, a login completed, the request then
    read the new session and made an upstream bars call without any token."""
    stack = Stack()
    parked_after_authorize = threading.Event()
    resume = threading.Event()
    real_authorize = stack.session.authorize

    def authorize_then_park(token):
        decided = real_authorize(token)  # no session bound: a tokenless caller passes
        parked_after_authorize.set()
        assert resume.wait(WAIT_S)
        return decided

    stack.session.authorize = authorize_then_park  # type: ignore[method-assign]
    client = stack.new_client()
    request = Parked(lambda: stack.bars(None, client=client)).start()
    assert parked_after_authorize.wait(WAIT_S)

    stack.session.authorize = real_authorize  # type: ignore[method-assign]
    logged_in = stack.login()  # a complete login lands while the request is parked
    assert stack.bars(logged_in).status_code == 200
    bars_before = len(stack.transport.bars_requests())
    resume.set()

    resp = request.join()
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert len(stack.transport.bars_requests()) == bars_before  # no upstream call for it


def test_the_relay_token_is_rechecked_after_an_egress_refresh():
    """The snapshot authorize() took is not enough once a refresh ran outside the
    lock: a revoke and a new login meanwhile must make the old bearer a 401."""
    stack = Stack()
    old = stack.login()
    stack.clock.advance(301)  # the egress check is stale: the bars request refreshes it
    entered, release = park_next_request(stack, is_echo)
    client = stack.new_client()
    request = Parked(lambda: stack.bars(old, client=client)).start()
    assert entered.wait(WAIT_S)

    assert stack.revoke(old).status_code == 200
    new = stack.login()
    bars_before = len(stack.transport.bars_requests())
    release.set()

    resp = request.join()
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert len(stack.transport.bars_requests()) == bars_before
    assert stack.bars(new).status_code == 200


def test_a_session_dropped_during_the_egress_refresh_is_a_503_not_a_200():
    stack = Stack()
    token = stack.login()
    stack.clock.advance(301)
    entered, release = park_next_request(stack, is_echo)
    client = stack.new_client()
    request = Parked(lambda: stack.bars(token, client=client)).start()
    assert entered.wait(WAIT_S)
    assert stack.revoke(token).status_code == 200
    bars_before = len(stack.transport.bars_requests())
    release.set()
    resp = request.join()
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert len(stack.transport.bars_requests()) == bars_before


def test_a_newer_attempt_that_completes_first_supersedes_the_older_exchange():
    """Codex P1 repro (a): B completes while A is exchanging; A must not install."""
    stack = Stack()
    first = stack.arm().json()
    entered, release = park_next_request(stack, is_customerdetails)
    older = start_complete(stack, first)
    assert entered.wait(WAIT_S)  # A is past its egress check and inside customerdetails

    second = stack.arm().json()
    assert stack.complete(second["attempt_id"], second["relay_token"]).status_code == 200
    release.set()

    done = older.join()
    assert done.status_code == 409 and code_of(done) == "LOGIN_ATTEMPT_INVALID"
    assert stack.bars(second["relay_token"]).status_code == 200  # B's token still works
    refused = stack.bars(first["relay_token"])  # A's token never became the session's
    assert refused.status_code == 401 and code_of(refused) == "UNAUTHORIZED"
    assert stack.session.state is SessionState.ACTIVE


def test_a_newer_arm_alone_supersedes_the_older_exchange():
    stack = Stack()
    first = stack.arm().json()
    entered, release = park_next_request(stack, is_customerdetails)
    older = start_complete(stack, first)
    assert entered.wait(WAIT_S)
    stack.arm()  # replaces the attempt; the exchange of the old one is still running
    release.set()
    done = older.join()
    assert done.status_code == 409 and code_of(done) == "LOGIN_ATTEMPT_INVALID"
    assert stack.session.state is SessionState.NONE
    assert stack.bars(first["relay_token"]).status_code == 503


def test_an_egress_suspension_during_the_exchange_blocks_the_older_login():
    """Codex P1 repro (b): B sees an egress mismatch while A is exchanging."""
    stack = Stack()
    first = stack.arm().json()
    entered, release = park_next_request(stack, is_customerdetails)
    older = start_complete(stack, first)
    assert entered.wait(WAIT_S)

    stack.transport.echo_ip = OTHER_IP
    second = stack.arm().json()
    detected = stack.complete(second["attempt_id"], second["relay_token"])
    assert detected.status_code == 503 and code_of(detected) == "EGRESS_MISMATCH"
    release.set()

    done = older.join()
    assert done.status_code == 503 and code_of(done) == "EGRESS_MISMATCH"
    assert reason_of(done) == "egress_mismatch"
    health = stack.client.get("/v1/health", headers=CONTRACT_HDR).json()
    assert health["state"] == "suspended_egress" and health["egress"]["ok"] is False
    n = len(stack.transport.bars_requests())
    for token in (first["relay_token"], second["relay_token"]):
        resp = stack.bars(token)
        assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert len(stack.transport.bars_requests()) == n


def test_a_revoke_during_the_exchange_blocks_the_login():
    stack = Stack()
    armed = stack.arm().json()
    entered, release = park_next_request(stack, is_customerdetails)
    exchange = start_complete(stack, armed)
    assert entered.wait(WAIT_S)
    assert stack.revoke(None).status_code == 200
    release.set()
    done = exchange.join()
    assert done.status_code == 409 and code_of(done) == "LOGIN_ATTEMPT_INVALID"
    assert stack.session.state is SessionState.NONE


def test_the_session_dying_during_the_exchange_blocks_the_login():
    """The generation also moves on a session-dead Breeze error (an expiry)."""
    stack = Stack()
    old = stack.login()
    armed = stack.arm(old).json()
    entered, release = park_next_request(stack, is_customerdetails)
    exchange = start_complete(stack, armed)
    assert entered.wait(WAIT_S)
    stack.clock.advance(15 * 3600)  # the old session passes its IST deadline
    assert stack.session.state is SessionState.EXPIRED
    release.set()
    done = exchange.join()
    assert done.status_code == 409 and code_of(done) == "LOGIN_ATTEMPT_INVALID"
    assert stack.session.state is SessionState.EXPIRED
