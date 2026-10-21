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
