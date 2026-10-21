"""Egress evidence ordering and the egress_check event (61-08 follow-up 1).

A check is stamped with the moment it started, a failure outranks an ok that
started before it was recorded, and the event log says which checks made no
request. Deterministic: transport gates and a fake clock, no sleeps.
"""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

import pytest
from test_gateway_relay_app import OTHER_IP, Stack, code_of
from test_gateway_relay_routes import MASTER, get, real_stack

from gateway_vm.egress import FAILED_RECHECK_COOLDOWN_S


@pytest.fixture()
def rs(tmp_path) -> Stack:
    return real_stack(tmp_path)


def echo_requests(stack: Stack) -> list:
    return [r for r in stack.transport.requests if urlsplit(r[1]).hostname == "api.ipify.org"]


class ParkedEcho:
    """Parks the first echo fetch after it has been answered, until released."""

    def __init__(self, stack: Stack) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True
        real = stack.transport.request

        def request(method, url, *, headers, body, timeouts, max_bytes):
            response = real(method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes)
            if self._armed and urlsplit(url).hostname == "api.ipify.org":
                self._armed = False
                self.entered.set()
                assert self.release.wait(timeout=10)
            return response

        stack.transport.request = request  # type: ignore[method-assign]


def run_parked_check(stack: Stack) -> tuple[threading.Thread, ParkedEcho]:
    parked = ParkedEcho(stack)
    worker = threading.Thread(target=stack.session._refresh_egress)
    worker.start()
    assert parked.entered.wait(timeout=10)
    return worker, parked


# ------------------------------------------------ a failure outranks an older ok


def test_a_slow_ok_that_started_before_a_fast_failure_does_not_replace_it(rs: Stack):
    worker, parked = run_parked_check(rs)  # the slow ok has read a good echo and waits
    rs.clock.advance(1)
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()  # the fast failing check completes first
    assert rs.session.snapshot(refresh_egress_if_stale=False)["egress"]["ok"] is False
    parked.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    assert rs.session.snapshot(refresh_egress_if_stale=False)["egress"]["ok"] is False  # still the failure
    resp = get(rs, MASTER)
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert rs.transport.master_requests() == []


def test_an_ok_that_started_after_the_failure_was_recorded_replaces_it(rs: Stack):
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()
    rs.transport.echo_ip = "203.0.113.7"
    rs.clock.advance(FAILED_RECHECK_COOLDOWN_S + 1)
    rs.session._refresh_egress()
    assert rs.session.snapshot(refresh_egress_if_stale=False)["egress"]["ok"] is True
    assert get(rs, MASTER).status_code == 200


def test_the_evidence_ages_from_the_start_of_the_check_not_its_end(rs: Stack):
    worker, parked = run_parked_check(rs)
    rs.clock.advance(250)  # the check takes 250 s to return
    parked.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(echo_requests(rs)) == 1
    rs.clock.advance(60)  # 60 s since it returned, 310 s since it started
    resp = get(rs, MASTER)
    assert resp.status_code == 200
    assert len(echo_requests(rs)) == 2  # the old evidence was not ridden


# ------------------------------------------------------- the egress_check event


def egress_events(stack: Stack) -> list[dict]:
    return stack.events_named("egress_check")


def test_a_real_check_logs_cached_false_and_a_reused_failure_logs_cached_true(rs: Stack):
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()
    rs.clock.advance(1)
    rs.session._refresh_egress()  # inside the 30 s cooldown: no request
    rs.clock.advance(FAILED_RECHECK_COOLDOWN_S)
    rs.session._refresh_egress()  # past it: a real check again
    assert [e["cached"] for e in egress_events(rs)] == [False, True, False]
    assert [e["result"] for e in egress_events(rs)] == ["rejected"] * 3
    assert len(echo_requests(rs)) == 2  # the cached event made no request


def test_an_ok_check_logs_cached_false(rs: Stack):
    rs.session._refresh_egress()
    (event,) = egress_events(rs)
    assert event["cached"] is False and event["result"] == "ok"


# ------------------------------- round 2: equal stamps, record time, kept result, cooldown


def run_in_thread(fn) -> tuple[threading.Thread, list]:
    out: list = []
    worker = threading.Thread(target=lambda: out.append(fn()))
    worker.start()
    return worker, out


class EchoGates:
    """Parks each echo fetch, in call order, after it has been answered, until released."""

    def __init__(self, stack: Stack, count: int) -> None:
        self.entered = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self._calls = 0
        self._lock = threading.Lock()
        real = stack.transport.request

        def request(method, url, *, headers, body, timeouts, max_bytes):
            response = real(method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes)
            if urlsplit(url).hostname == "api.ipify.org":
                with self._lock:
                    index, self._calls = self._calls, self._calls + 1
                if index < count:
                    self.entered[index].set()
                    assert self.release[index].wait(timeout=10)
            return response

        stack.transport.request = request  # type: ignore[method-assign]


def test_an_ok_that_started_at_the_same_instant_as_a_failure_is_dropped(rs: Stack):
    worker, parked = run_parked_check(rs)
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()  # the clock has not moved: equal stamps
    parked.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert rs.session.snapshot(refresh_egress_if_stale=False)["egress"]["ok"] is False
    resp = get(rs, MASTER)
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert rs.transport.master_requests() == []


def test_a_failure_is_stamped_when_it_is_recorded_not_when_it_started(rs: Stack):
    gates = EchoGates(rs, 2)
    rs.transport.echo_ip = OTHER_IP
    failing, _ = run_in_thread(rs.session._refresh_egress)  # enters ipify at t0 and waits
    assert gates.entered[0].wait(timeout=10)
    rs.clock.advance(5)
    rs.transport.echo_ip = "203.0.113.7"
    passing, _ = run_in_thread(rs.session._refresh_egress)  # starts at t0+5, saw a good echo
    assert gates.entered[1].wait(timeout=10)
    rs.clock.advance(1)
    gates.release[0].set()  # the failure records at t0+6, after the ok started
    failing.join(timeout=10)
    assert not failing.is_alive()
    gates.release[1].set()
    passing.join(timeout=10)
    assert not passing.is_alive()

    assert rs.session.snapshot(refresh_egress_if_stale=False)["egress"]["ok"] is False
    resp = get(rs, MASTER)
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert rs.transport.master_requests() == []


def test_run_egress_returns_the_stored_failure_not_the_dropped_ok(rs: Stack):
    parked = ParkedEcho(rs)
    slow, slow_out = run_in_thread(rs.session._run_egress)
    assert parked.entered.wait(timeout=10)
    rs.clock.advance(1)
    rs.transport.echo_ip = OTHER_IP
    fast = rs.session._run_egress()
    parked.release.set()
    slow.join(timeout=10)
    assert not slow.is_alive()
    assert fast.ok is False
    (returned,) = slow_out
    assert returned.ok is False and returned == fast


def test_the_event_for_a_dropped_ok_shows_the_result_the_gate_kept(rs: Stack):
    worker, parked = run_parked_check(rs)
    rs.clock.advance(1)
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()
    parked.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    events = egress_events(rs)
    assert [e["result"] for e in events] == ["rejected", "rejected"]  # the ok line says what was kept
    assert [e["reason"] for e in events] == ["egress_mismatch", "egress_mismatch"]
    assert [e["cached"] for e in events] == [False, False]


def test_a_dropped_ok_does_not_end_the_failure_cooldown(rs: Stack):
    worker, parked = run_parked_check(rs)  # one echo request so far
    rs.clock.advance(1)
    rs.transport.echo_ip = OTHER_IP
    rs.session._refresh_egress()  # two echo requests; the failure starts its cooldown
    parked.release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(echo_requests(rs)) == 2
    rs.clock.advance(1)
    result = rs.session._refresh_egress()  # still inside 30 s of the failure
    assert result.ok is False
    assert len(echo_requests(rs)) == 2  # no new request: the cooldown survived the dropped ok
    assert egress_events(rs)[-1]["cached"] is True
