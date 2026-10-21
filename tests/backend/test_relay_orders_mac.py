"""63-05 Task 3: the Mac's relay order pieces on their own (no VM, no network, no ledger).

Parsers for the relay's GET reads, the strict O3 intent builder, the minted-bytes checker and the
dispatcher's outcome mapping. Everything here is synthetic. The full flow over the real VM app is
``test_relay_orders_e2e.py``.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from brokers.breeze_gateway import india_quote as iq
from brokers.breeze_gateway.errors import (
    LimitRejected,
    NoSession,
    OrdersBlocked,
    OrdersUnavailable,
    RelayProtocolError,
    RelayUnavailable,
    Replay,
    SignatureInvalid,
)
from brokers.breeze_gateway.orders import (
    DECISION_NOT_FORWARDED,
    AuthorizeResult,
    MintMismatch,
    canonical_bytes,
    check_minted_bytes,
    intent_body,
    intent_sha256,
)
from execution.relay_dispatcher import (
    OUTCOME_NOT_FORWARDED,
    OUTCOME_REFUSED,
    OUTCOME_UNKNOWN,
    RELAY_FORWARD_DISABLED,
    RelayDispatcher,
    reason_code,
)
from risk_india import rules

HEX_A, HEX_B, HEX_C = "a" * 64, "b" * 64, "c" * 64
CHALLENGE = "11111111-2222-4333-8444-555555555555"


def success(rows) -> bytes:
    return json.dumps({"Success": rows, "Status": 200, "Error": None}).encode()


# ------------------------------------------------------------------------- quote


QUOTE_ROW = {
    "stock_code": "TESTCO",
    "ltp": 100.07,
    "best_bid_price": 100.05,
    "best_offer_price": 100.1,
    "upper_circuit": 110.0,
    "lower_circuit": 90.0,
    "previous_close": 99.8,
    "ltt": "21-Oct-2026 09:59:30",
}


def test_a_good_quote_parses_to_exact_decimals():
    quote = iq.parse_quote(success([QUOTE_ROW]), "TESTCO")
    assert quote.ltp == Decimal("100.07") and quote.bid == Decimal("100.05")
    assert quote.session_date == date(2026, 10, 21)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(ltp="100.07"),  # a quoted price is not the shape the smoke saw
        lambda r: r.update(ltp=0),
        lambda r: r.update(previous_close=-1),
        lambda r: r.update(stock_code="OTHER"),
        lambda r: r.pop("ltt"),
        lambda r: r.update(ltt="2026-10-21"),
        lambda r: r.update(ltp=True),
        lambda r: r.update(lower_circuit=None),
    ],
)
def test_an_untrusted_quote_shape_is_quote_unavailable(mutate):
    row = dict(QUOTE_ROW)
    mutate(row)
    with pytest.raises(iq.QuoteUnavailable):
        iq.parse_quote(success([row]), "TESTCO")


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"Success": NaN}', b"[]", b'{"Status": 200}', success([QUOTE_ROW, QUOTE_ROW]), success([])],
)
def test_a_broken_or_ambiguous_quote_body_is_quote_unavailable(body):
    with pytest.raises(iq.QuoteUnavailable):
        iq.parse_quote(body, "TESTCO")


def test_a_missing_or_zero_book_side_is_absent_not_a_price():
    row = dict(QUOTE_ROW, best_bid_price=0, best_offer_price=None)
    quote = iq.parse_quote(success([row]), "TESTCO")
    assert quote.bid is None and quote.ask is None


def bar(day: str, close) -> dict:
    return {"datetime": f"{day} 00:00:00", "close": close}


def test_bar_closes_parse_by_date():
    closes = iq.parse_bar_closes(success([bar("2026-10-19", 99.5), bar("2026-10-20", "100.25")]))
    assert closes == {date(2026, 10, 19): Decimal("99.5"), date(2026, 10, 20): Decimal("100.25")}


@pytest.mark.parametrize(
    "rows",
    [
        [bar("2026-10-19", 1), bar("2026-10-19", 2)],  # a repeated date
        [bar("2026-10-19", 0)],
        [{"datetime": "2026-10-19 09:15:00", "close": 1}],  # not a daily stamp
        [{"datetime": "2026-10-19 00:00:00"}],  # no close
        ["x"],
        [bar("2026-02-30", 1)],  # not a date
    ],
)
def test_untrusted_bars_are_refused(rows):
    with pytest.raises(iq.QuoteUnavailable):
        iq.parse_bar_closes(success(rows))


def test_a_response_at_the_row_cap_may_have_been_cut_and_is_never_complete():
    rows = [bar(date.fromordinal(738000 + i).isoformat(), 1) for i in range(iq.BARS_ROWS_CAP)]
    with pytest.raises(iq.QuoteUnavailable):
        iq.parse_bar_closes(success(rows))


# ------------------------------------------------------------------------ master


def master(rows, *, member="NSEScripMaster.txt", header="Token,ShortName,Series,ISINCode") -> bytes:
    text = header + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, text)
    return buffer.getvalue()


def test_master_keeps_live_eq_and_be_rows_and_drops_the_rest():
    parsed = iq.parse_master(
        master(
            [
                ("1", "AAA", "EQ", "INE000A01012"),
                ("2", "BBB", "BE", "INE111B01023"),
                ("3", "CCC", "BZ", "INE222C01034"),  # not a cash series
                ("0", "NIFTY", "EQ", "INE999Z99999"),  # an index line
                ("x", "DDD", "EQ", "INE333D01045"),  # no numeric token
            ]
        )
    )
    assert set(parsed) == {"AAA", "BBB"}
    assert parsed["AAA"].isin == "INE000A01012" and parsed["BBB"].series == "BE"


def test_master_leaves_out_an_ambiguous_code_and_a_shared_isin():
    parsed = iq.parse_master(
        master(
            [
                ("1", "AAA", "EQ", "INE000A01012"),
                ("2", "AAA", "BE", "INE111B01023"),  # a second live row: ambiguous
                ("3", "BBB", "EQ", "INE222C01034"),
                ("4", "CCC", "EQ", "INE222C01034"),  # the same ISIN under two codes
                ("5", "EEE", "EQ", "BADISIN"),  # a live cash row with no usable ISIN
                ("6", "FFF", "EQ", "INE444F01056"),
            ]
        )
    )
    assert set(parsed) == {"FFF"}


@pytest.mark.parametrize(
    "content",
    [
        b"not a zip",
        master([("1", "AAA", "EQ", "INE000A01012")], member="Other.txt"),
        master([("1", "AAA", "EQ", "INE000A01012")], header="Token,ShortName,Series"),
        master([("1", "AAA", "EQ")]),  # a short row
    ],
    # Zip bytes embed the build time, so the default ids differ between xdist workers.
    ids=["not-a-zip", "wrong-member", "missing-column", "short-row"],
)
def test_an_unreadable_master_is_refused(content):
    with pytest.raises(iq.QuoteUnavailable):
        iq.parse_master(content)


@pytest.mark.parametrize(
    ("reference", "ltp", "expected"),
    [
        ("99.8", "100.07", "100.07"),  # a 0.01 tick leaves a two-decimal price alone
        ("250", "300.07", "300.05"),  # a 0.05 tick floors
        ("250", "300.04", "300.00"),
        ("1500", "1500.19", "1500.10"),  # a 0.10 tick floors
    ],
)
def test_the_limit_price_is_the_ltp_floored_to_the_nse_tick(reference, ltp, expected):
    quote = rules.Quote(
        stock_code="TESTCO", isin="INE000A01012", series="EQ", ltp=Decimal(ltp),
        lower_circuit=Decimal("1"), upper_circuit=Decimal("9999"),
        previous_close=Decimal(reference), session_date=date(2026, 10, 21),
        tick_reference=Decimal(reference), tick_reference_month=date(2026, 9, 30),
        bid=Decimal("99.9"), ask=Decimal("100.1"),
    )
    price = iq.aligned_limit(quote)
    assert price == Decimal(expected) and price <= quote.ltp  # never above the last price
    assert price.as_tuple().exponent == -2  # exactly two decimals, as the VM's intent needs


def test_no_tick_reference_means_no_limit_price():
    quote = rules.Quote(
        stock_code="TESTCO", isin="INE000A01012", series="EQ", ltp=Decimal("100"),
        lower_circuit=Decimal("90"), upper_circuit=Decimal("110"),
        previous_close=Decimal("99.8"), session_date=date(2026, 10, 21),
        tick_reference=None, tick_reference_month=None, bid=None, ask=None,
    )
    with pytest.raises(iq.QuoteUnavailable):
        iq.aligned_limit(quote)


# ----------------------------------------------------------- the O3 intent builder


GOOD = dict(
    intent_id="i" + "0" * 40, proposal_id="p-1", side="buy", stock_code="TESTCO",
    isin="INE000A01012", quantity=10, limit_price="100.05", reason="entry", batch_id=None,
    limits_sha256=HEX_A, params_sha256=HEX_B, key_id=HEX_C,
)


def test_the_intent_body_has_exactly_the_o3_keys():
    body = intent_body(**GOOD)
    assert body["mode"] == "SHADOW" and body["workspace"] == "india" and body["broker"] == "icici-breeze"
    assert len(body) == 19 and body["quantity"] == 10 and body["limit_price"] == "100.05"


@pytest.mark.parametrize(
    "override",
    [
        {"side": "BUY"},
        {"reason": "panic"},
        {"quantity": 0},
        {"quantity": 1.0},
        {"quantity": True},
        {"quantity": 100_001},
        {"limit_price": "100.055"},
        {"limit_price": "0"},
        {"limit_price": 100.05},
        {"limit_price": "-1"},
        {"isin": "bad"},
        {"stock_code": "lower"},
        {"limits_sha256": "xyz"},
        {"key_id": HEX_A.upper()},
        {"batch_id": "has space"},
    ],
)
def test_a_body_the_vm_would_refuse_is_refused_before_it_is_sent(override):
    with pytest.raises(MintMismatch):
        intent_body(**{**GOOD, **override})


def test_intent_sha256_is_the_hash_of_the_canonical_bytes():
    body = intent_body(**GOOD)
    assert intent_sha256(body) == hashlib.sha256(canonical_bytes(body)).hexdigest()
    assert canonical_bytes({"b": 1, "a": [1, "x"]}) == b'{"a":[1,"x"],"b":1}'
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})


# --------------------------------------------------------- the minted-bytes checker


def signed_bytes(**overrides) -> tuple[bytes, dict]:
    body = intent_body(**GOOD)
    payload = {
        "version": 1,
        "purpose": "growin.relay.order",
        "challenge_id": CHALLENGE,
        "nonce": "n" * 43,
        "issued_at": 1_792_557_000,
        "expires_at": 1_792_557_060,
        "key_id": HEX_C,
        "limits_sha256": HEX_A,
        "intent": body,
    }
    payload.update(overrides)
    return canonical_bytes(payload), body


def check(signed: bytes, body: dict, **kwargs):
    args = dict(challenge_id=CHALLENGE, expected_intent=body, limits_sha256=HEX_A, key_id=HEX_C)
    args.update(kwargs)
    return check_minted_bytes(signed, **args)


def test_minted_bytes_that_match_pass_and_report_their_times():
    signed, body = signed_bytes()
    checked = check(signed, body)
    assert checked.signed_bytes == signed and checked.challenge_id == CHALLENGE
    assert (checked.issued_at_epoch, checked.expires_at_epoch) == (1_792_557_000, 1_792_557_060)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"challenge_id": "00000000-0000-4000-8000-000000000000"},
        {"limits_sha256": HEX_B},
        {"key_id": HEX_B},
    ],
)
def test_minted_bytes_must_match_the_macs_own_ids_and_hashes(kwargs):
    signed, body = signed_bytes()
    with pytest.raises(MintMismatch):
        check(signed, body, **kwargs)


def test_minted_bytes_must_embed_the_macs_own_intent():
    signed, body = signed_bytes()
    with pytest.raises(MintMismatch):
        check(signed, {**body, "quantity": 11})


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "short"},
        {"issued_at": "1"},
        {"expires_at": 1_792_557_000},  # not after issued_at
        {"expires_at": 1_792_557_000 + 301},  # a lifetime over five minutes
        {"version": 2},
        {"purpose": "growin.execution.dispatch"},
        {"challenge_id": "short"},
    ],
)
def test_minted_bytes_with_a_bad_envelope_are_refused(overrides):
    signed, body = signed_bytes(**overrides)
    with pytest.raises(MintMismatch):
        check(signed, body)


# ------------------------------------------------------------ the dispatcher mapping


class _Orders:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.sent = 0

    def authorize(self, challenge_id, signature_der):
        self.sent += 1
        if isinstance(self._behaviour, Exception):
            raise self._behaviour
        return self._behaviour


class _Link:
    def __init__(self, behaviour):
        self.orders_client = _Orders(behaviour)

    def orders(self):
        return self.orders_client


class _BrokenLink:
    def orders(self):
        raise OSError("tunnel will not open")


def payload_bytes() -> bytes:
    return signed_bytes()[0]


async def run(behaviour):
    link = _Link(behaviour)
    outcome = await RelayDispatcher(link).dispatch_signed(
        object(), challenge_id=CHALLENGE, signature_der=b"\x30\x00", signed_payload=payload_bytes()
    )
    return outcome, link.orders_client.sent


@pytest.mark.asyncio
async def test_verified_not_forwarded_is_a_definite_non_placement():
    outcome, sent = await run(AuthorizeResult(DECISION_NOT_FORWARDED, "i1", 2, HEX_A))
    assert (outcome.kind, outcome.reason_code) == (OUTCOME_NOT_FORWARDED, RELAY_FORWARD_DISABLED)
    assert outcome.placed is False and outcome.audit_seq == 2 and sent == 1
    assert outcome.intent_sha256 == intent_sha256(signed_bytes()[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Replay(reason="intent_consumed"), "intent_consumed"),
        (LimitRejected(reason="collar"), "collar"),
        (OrdersBlocked(reason="kill_switch"), "kill_switch"),
        (SignatureInvalid(reason="signature_invalid"), "signature_invalid"),
        (NoSession(), "no_session"),
        (OrdersUnavailable(reason="audit_broken", http_status=503), "audit_broken"),
    ],
)
async def test_a_vm_refusal_is_a_definite_refusal_with_its_o6_code(error, code):
    outcome, sent = await run(error)
    assert (outcome.kind, outcome.reason_code, outcome.decision) == (OUTCOME_REFUSED, code, "REFUSED")
    assert sent == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RelayUnavailable(reason="timeout"),
        RelayUnavailable(reason="connect_error"),
        RelayProtocolError(reason="bad_field"),
        RuntimeError("boom"),
    ],
)
async def test_an_untrusted_or_missing_answer_is_unknown_and_sent_once(error):
    outcome, sent = await run(error)
    assert outcome.kind == OUTCOME_UNKNOWN and outcome.placed is False
    assert sent == 1  # never retried


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["tunnel_down", "tunnel_restart_backoff", "tunnel_restart_failed"])
async def test_a_request_that_provably_never_left_is_a_definite_refusal(reason):
    outcome, _sent = await run(RelayUnavailable(reason=reason))
    assert (outcome.kind, outcome.reason_code) == (OUTCOME_REFUSED, "RELAY_UNAVAILABLE")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["closed", "tunnel_restarted"])
async def test_a_closed_or_restarted_client_may_have_sent_so_it_is_unknown(reason):
    # The client raises these from the RuntimeError httpx throws after a send as well as before
    # one, so they prove nothing. Fix r1: they used to count as "never left" and settle FAILED
    # while the VM had already logged VERIFIED_NOT_FORWARDED.
    outcome, sent = await run(RelayUnavailable(reason=reason))
    assert (outcome.kind, outcome.reason_code) == (OUTCOME_UNKNOWN, "BROKER_TIMEOUT")
    assert sent == 1  # never retried


@pytest.mark.asyncio
async def test_a_decision_the_mac_does_not_know_is_unknown_never_placed():
    outcome, _sent = await run(AuthorizeResult("FORWARDED", "i1", 2, HEX_A))
    assert (outcome.kind, outcome.reason_code) == (OUTCOME_UNKNOWN, "RELAY_UNEXPECTED_DECISION")
    assert outcome.placed is False


@pytest.mark.asyncio
async def test_a_link_that_will_not_open_sends_nothing_and_refuses():
    outcome = await RelayDispatcher(_BrokenLink()).dispatch_signed(
        object(), challenge_id=CHALLENGE, signature_der=b"\x30\x00", signed_payload=payload_bytes()
    )
    assert (outcome.kind, outcome.reason_code) == (OUTCOME_REFUSED, "RELAY_UNAVAILABLE")


def test_reason_codes_are_sanitised_for_the_ledger():
    assert reason_code("kill_switch", "x") == "kill_switch"
    assert reason_code("a:b", "x") == "a.b"
    assert reason_code("has space", "x") == "x"
    assert reason_code(None, "x") == "x"
    assert reason_code("z" * 65, "x") == "x"
