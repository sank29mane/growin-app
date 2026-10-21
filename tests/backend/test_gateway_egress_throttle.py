"""EgressChecker: a failing check is not repeated per request (PR #582 round 2).

Injected clock, a counting transport. Only failures are reused; a success is
never cached by the checker and a failed result is never turned into a pass.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from test_gateway_relay_app import EXPECTED_IP, OTHER_IP, FakeClock, FakeTransport

from gateway_vm.egress import FAILED_RECHECK_COOLDOWN_S, EgressChecker


def make(*, echo: str = EXPECTED_IP) -> tuple[EgressChecker, FakeTransport, FakeClock]:
    clock, transport = FakeClock(), FakeTransport()
    transport.echo_ip = echo
    checker = EgressChecker(transport, expected_ip=EXPECTED_IP, echo_url="https://api.ipify.org", clock=clock)
    return checker, transport, clock


def probes(transport: FakeTransport) -> int:
    return len([r for r in transport.requests if urlsplit(r[1]).hostname == "api.ipify.org"])


def test_the_default_cooldown_is_30_seconds():
    assert FAILED_RECHECK_COOLDOWN_S == 30.0


def test_a_failed_check_is_reused_inside_the_window_without_any_request():
    checker, transport, clock = make(echo=OTHER_IP)
    first = checker.check()
    assert first.ok is False and probes(transport) == 1
    for _ in range(20):
        clock.advance(1)
        again = checker.check()
        assert again.ok is False and again is first  # fails closed, same result
    assert probes(transport) == 1


def test_a_failed_check_is_probed_again_after_the_window():
    checker, transport, clock = make(echo=OTHER_IP)
    checker.check()
    clock.advance(30)
    checker.check()
    assert probes(transport) == 2


def test_inside_the_window_a_fixed_echo_still_reports_the_failure_never_a_pass():
    checker, transport, clock = make(echo=OTHER_IP)
    assert checker.check().ok is False
    transport.echo_ip = EXPECTED_IP
    clock.advance(5)
    assert checker.check().ok is False  # fail closed until the window passes
    clock.advance(30)
    assert checker.check().ok is True


def test_a_success_is_never_reused_by_the_checker():
    checker, transport, clock = make()
    assert checker.check().ok is True and checker.check().ok is True
    assert probes(transport) == 2


def test_a_success_clears_the_failure_window():
    checker, transport, clock = make(echo=OTHER_IP)
    checker.check()
    clock.advance(31)
    transport.echo_ip = EXPECTED_IP
    assert checker.check().ok is True
    transport.echo_ip = OTHER_IP
    assert checker.check().ok is False  # a new failure after a success probes at once
    assert probes(transport) == 3
