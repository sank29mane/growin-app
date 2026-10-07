"""The Trading 212 governor (UKT-03, D-14): pinned table, spacing, holds, no randomness."""

import asyncio

import pytest

from brokers.trading212.governor import (
    LIMIT_TABLE,
    EndpointLimit,
    Governor,
    UnknownEndpointError,
    endpoint_template,
)
from t212_testkit import FakeClock

# The research section 3 table, restated here so a change to the code table is a diff
# in two places. Pies reads are not in that table; see the governor's comment.
PUBLISHED = {
    "POST /equity/orders/limit": (1, 2.0),
    "POST /equity/orders/stop": (1, 2.0),
    "POST /equity/orders/stop_limit": (1, 2.0),
    "POST /equity/orders/market": (50, 60.0),
    "DELETE /equity/orders/{id}": (50, 60.0),
    "GET /equity/orders/{id}": (1, 1.0),
    "GET /equity/positions": (1, 1.0),
    "GET /equity/orders": (1, 5.0),
    "GET /equity/account/summary": (1, 5.0),
    "GET /equity/history/orders": (20, 60.0),
    "GET /equity/history/dividends": (20, 60.0),
    "GET /equity/history/transactions": (20, 60.0),
    "GET /equity/metadata/instruments": (1, 50.0),
    "GET /equity/metadata/exchanges": (1, 30.0),
    "POST /equity/history/exports": (1, 30.0),
    "GET /equity/history/exports": (1, 60.0),
}
DERIVED_PINS = {"GET /equity/pies": (1, 30.0), "GET /equity/pies/{id}": (1, 30.0)}


def make() -> tuple[Governor, FakeClock]:
    clock = FakeClock()
    return Governor(clock=clock, sleep=clock.sleep), clock


def test_the_limit_table_is_pinned_to_the_published_values():
    expected = {**PUBLISHED, **DERIVED_PINS}
    actual = {key: (value.limit, value.period) for key, value in LIMIT_TABLE.items()}
    assert actual == expected


@pytest.mark.asyncio
async def test_two_limit_order_posts_are_two_seconds_apart_on_the_fake_clock():
    governor, clock = make()
    await governor.acquire("POST", "equity/orders/limit")
    first = clock.now
    await governor.acquire("POST", "equity/orders/limit")
    assert clock.now - first == pytest.approx(2.0)
    assert clock.sleeps == [pytest.approx(2.0)]


@pytest.mark.asyncio
async def test_the_first_acquire_on_a_key_never_waits():
    governor, clock = make()
    await governor.acquire("GET", "equity/account/summary")
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_order_by_id_reads_for_different_ids_share_one_key():
    governor, clock = make()
    assert endpoint_template("GET", "equity/orders/1") == endpoint_template(
        "GET", "/api/v0/equity/orders/2?x=1"
    )
    assert endpoint_template("GET", "equity/orders/1") == "GET /equity/orders/{id}"
    first = await governor.acquire("GET", "equity/orders/1")
    start = clock.now
    second = await governor.acquire("GET", "equity/orders/2")
    assert first == second
    assert clock.now - start == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_pending_list_and_order_by_id_are_different_keys():
    governor, clock = make()
    await governor.acquire("GET", "equity/orders")
    await governor.acquire("GET", "equity/orders/9")
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_a_zero_remaining_header_holds_the_key_until_reset():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/positions")
    reset = clock.now + 30.0
    governor.observe(key, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
    await governor.acquire("GET", "equity/positions")
    assert clock.now == pytest.approx(reset)
    assert clock.sleeps == [pytest.approx(30.0)]


@pytest.mark.asyncio
async def test_a_nonzero_remaining_header_does_not_hold():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/history/orders")
    governor.observe(key, {"x-ratelimit-remaining": "7", "x-ratelimit-reset": str(clock.now + 500)})
    await governor.acquire("GET", "equity/history/orders")
    assert clock.sleeps == [pytest.approx(3.0)]  # spacing only: 60 s / 20


@pytest.mark.asyncio
async def test_header_names_are_case_insensitive():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/positions")
    governor.observe(key, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(clock.now + 12)})
    await governor.acquire("GET", "equity/positions")
    assert clock.sleeps == [pytest.approx(12.0)]


@pytest.mark.asyncio
async def test_zero_remaining_with_no_reset_holds_for_one_period():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/account/summary")
    governor.observe(key, {"x-ratelimit-remaining": "0"})
    await governor.acquire("GET", "equity/account/summary")
    assert clock.sleeps == [pytest.approx(5.0)]


@pytest.mark.asyncio
async def test_hold_never_shortens_an_existing_hold():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/positions")
    governor.observe(key, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(clock.now + 40)})
    governor.observe(key, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(clock.now + 5)})
    await governor.acquire("GET", "equity/positions")
    assert clock.sleeps == [pytest.approx(40.0)]


@pytest.mark.asyncio
async def test_a_throttle_waits_for_the_reset_header_when_it_is_in_the_future():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/account/summary")
    start = clock.now
    wait = governor.hold_after_throttle(key, {"x-ratelimit-reset": str(start + 30)})
    assert wait == pytest.approx(30.0)
    await governor.acquire("GET", "equity/account/summary")
    assert clock.sleeps == [pytest.approx(30.0)]
    assert clock.now == pytest.approx(start + 30.0)


@pytest.mark.asyncio
async def test_a_short_reset_never_undercuts_the_spacing():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/account/summary")
    start = clock.now
    governor.hold_after_throttle(key, {"x-ratelimit-reset": str(start + 3)})
    await governor.acquire("GET", "equity/account/summary")
    assert clock.now == pytest.approx(start + 5.0)


@pytest.mark.asyncio
async def test_a_throttle_without_a_usable_reset_waits_one_full_period():
    governor, clock = make()
    key = await governor.acquire("GET", "equity/positions")
    assert governor.hold_after_throttle(key, {}) == pytest.approx(1.0)
    stale = governor.hold_after_throttle(key, {"x-ratelimit-reset": str(clock.now - 100)})
    assert stale == pytest.approx(1.0)
    assert governor.hold_after_throttle(key, {"x-ratelimit-reset": "soon"}) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_market_posts_use_the_fifty_per_minute_spacing():
    governor, clock = make()
    await governor.acquire("POST", "equity/orders/market")
    await governor.acquire("POST", "equity/orders/market")
    assert clock.sleeps == [pytest.approx(1.2)]


@pytest.mark.asyncio
async def test_keys_do_not_wait_on_each_other():
    governor, clock = make()
    await governor.acquire("POST", "equity/orders/limit")
    await governor.acquire("GET", "equity/positions")
    await governor.acquire("GET", "equity/account/summary")
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_concurrent_callers_on_one_key_are_spaced_evenly():
    governor, clock = make()
    stamps: list[float] = []

    async def one():
        await governor.acquire("POST", "equity/orders/limit")
        stamps.append(clock.now)

    await asyncio.gather(one(), one(), one())
    assert [round(b - a, 6) for a, b in zip(stamps, stamps[1:])] == [2.0, 2.0]


@pytest.mark.asyncio
async def test_each_governor_keeps_its_own_account_budget():
    first, clock_a = make()
    second, clock_b = make()
    await first.acquire("GET", "equity/positions")
    await second.acquire("GET", "equity/positions")
    assert clock_a.sleeps == [] and clock_b.sleeps == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "equity/account/info"),
        ("GET", "equity/account/cash"),
        ("GET", "equity/portfolio"),
        ("POST", "equity/positions"),
        ("PUT", "equity/orders/limit"),
        ("GET", ""),
        ("GET", "something/else"),
    ],
)
@pytest.mark.asyncio
async def test_an_unknown_endpoint_raises_and_never_sleeps(method, path):
    governor, clock = make()
    with pytest.raises(UnknownEndpointError):
        await governor.acquire(method, path)
    assert clock.sleeps == []


def test_an_unknown_template_raises_on_observe_and_throttle():
    governor, _ = make()
    with pytest.raises(UnknownEndpointError):
        governor.observe("GET /equity/portfolio", {"x-ratelimit-remaining": "0"})
    with pytest.raises(UnknownEndpointError):
        governor.hold_after_throttle("GET /equity/portfolio", {})


def test_every_pattern_has_exactly_one_table_row():
    import brokers.trading212.governor as module

    assert {template for _, _, template in module._PATTERNS} == set(LIMIT_TABLE)


def test_the_interval_is_period_over_limit():
    assert EndpointLimit(20, 60.0).interval == pytest.approx(3.0)
    assert EndpointLimit(1, 2.0).interval == pytest.approx(2.0)
