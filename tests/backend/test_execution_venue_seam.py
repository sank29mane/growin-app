"""Phase 66-01: the dispatcher seam, OrderMode.PRACTICE and the practice ledger.

No test here contacts a broker. The practice dispatcher is a recording double
registered in an injected factory map; the production map holds only ``paper``.
All limit values are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app_context import AppState
from execution import (
    ApprovalConflict,
    ApprovalService,
    ExecutionDisabledError,
    ExecutionLedger,
    ExecutionService,
    OrderIntent,
    OrderMode,
    PaperDispatcher,
    VenueBinding,
    default_ledger_path,
    practice_ledger_path,
)
from execution.ledger import LedgerVenueMismatch, canonical_json
from execution.venue import (
    ACCOUNT_BINDING_MISMATCH,
    BROKER_VENUE_MISMATCH,
    LIVE_DISABLED,
    MODE_VENUE_MISMATCH,
    VENUE_PAPER,
    VENUE_T212_PRACTICE,
    VenueError,
    execution_mode_label,
    production_dispatcher_factories,
    refusal_text,
)
from venue_seam_testkit import (
    SYNTH_ACCOUNT,
    SYNTH_LIMITS,
    RecordingDispatcher,
    enroll,
    practice_execution_payload,
    practice_factories,
    practice_proposal,
    prepare,
    private_key,
    sign,
    write_json,
    write_practice_files,
)


@pytest.fixture
def uk_process(monkeypatch):
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")


def test_production_factory_map_holds_only_paper():
    assert set(production_dispatcher_factories()) == {VENUE_PAPER}


@pytest.mark.parametrize("venue", ["t212_live", "", "PAPER", "breeze_relay", None])
def test_resolving_an_unknown_venue_raises_and_never_returns_the_paper_factory(venue):
    from execution.venue import resolve_factory, select_dispatcher, VenueContext

    with pytest.raises(VenueError) as refused:
        resolve_factory(venue)
    assert refused.value.code == "VENUE_UNKNOWN"
    with pytest.raises(VenueError):
        select_dispatcher(VenueContext(workspace="uk", venue=venue))


def test_resolving_a_known_but_unregistered_venue_raises_unavailable():
    from execution.venue import resolve_factory

    with pytest.raises(VenueError) as refused:
        resolve_factory(VENUE_T212_PRACTICE)
    assert refused.value.code == "VENUE_UNAVAILABLE"
    assert resolve_factory(VENUE_PAPER)(None).__class__.__name__ == "PaperDispatcher"


@pytest.mark.asyncio
async def test_tracer_signed_practice_intent_reaches_the_seam_double_once(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    double = RecordingDispatcher()
    ledger_path = tmp_path / "practice" / "execution.sqlite3"
    app_state = AppState()

    assert app_state.start_execution(
        ledger_path,
        workspace="uk",
        private_dir=private_config_dir,
        dispatcher_factories=practice_factories(double),
    ), app_state.execution_startup_error
    try:
        service = app_state.execution_service
        ledger: ExecutionLedger = app_state._execution_ledger
        assert ledger.path == ledger_path
        assert ledger.venue_binding is not None
        assert ledger.venue_binding.account_id == SYNTH_ACCOUNT

        key = private_key()
        enroll(service._approval_service, key)
        proposal = practice_proposal()
        admission = prepare(app_state, proposal)
        assert admission.decision.value == "ADMITTED"
        assert ledger.get_reservation("practice-1").state == "ACTIVE"

        challenge = service.create_approval_challenge("practice-1", workspace="uk")
        ack = await service.approve_signed(
            "practice-1",
            challenge.challenge_id,
            sign(key, challenge.signed_payload),
            workspace="uk",
        )

        assert len(double.intents) == 1
        sent = double.intents[0]
        assert sent.mode is OrderMode.PRACTICE
        assert (sent.ticker, sent.side.value, str(sent.quantity), str(sent.limit_price)) == (
            "VODl_EQ",
            "BUY",
            "2",
            "50",
        )
        assert sent.account == SYNTH_ACCOUNT
        assert ack.broker == VENUE_T212_PRACTICE
        stored = ledger.get_order("practice-1")
        assert stored.state == "ACKNOWLEDGED"
        assert stored.acknowledgment.broker_order_id == "practice-order-1"
        assert app_state.execution_mode == "practice"
    finally:
        app_state.close_execution()


# --- mode / venue matrix (D-08) ------------------------------------------------

BINDING = VenueBinding(venue=VENUE_T212_PRACTICE, account_id=SYNTH_ACCOUNT, currency="GBP")


def _intent(proposal_id: str, *, mode: str, broker: str, account: str) -> OrderIntent:
    extra = {}
    if mode == "PRACTICE":
        extra = {"order_type": "LIMIT", "limit_price": Decimal("50")}
    return OrderIntent(
        proposal_id=proposal_id,
        workspace="uk",
        account=account,
        broker=broker,
        mode=mode,
        ticker="VODl_EQ",
        side="BUY",
        quantity=Decimal("2"),
        **extra,
    )


# (id, ledger kind, mode, broker, account, refusal code or None)
MATRIX = [
    ("paper-paper", "paper", "PAPER", "paper", "invest", None),
    ("paper-practice", "paper", "PRACTICE", VENUE_T212_PRACTICE, SYNTH_ACCOUNT, MODE_VENUE_MISMATCH),
    ("paper-live", "paper", "LIVE", "trading212", "invest", LIVE_DISABLED),
    ("practice-practice", "practice", "PRACTICE", VENUE_T212_PRACTICE, SYNTH_ACCOUNT, None),
    ("practice-paper", "practice", "PAPER", "paper", SYNTH_ACCOUNT, MODE_VENUE_MISMATCH),
    ("practice-live", "practice", "LIVE", VENUE_T212_PRACTICE, SYNTH_ACCOUNT, LIVE_DISABLED),
    (
        "practice-other-account",
        "practice",
        "PRACTICE",
        VENUE_T212_PRACTICE,
        "acct-other-0002",
        ACCOUNT_BINDING_MISMATCH,
    ),
    (
        "practice-other-broker",
        "practice",
        "PRACTICE",
        "paper",
        SYNTH_ACCOUNT,
        BROKER_VENUE_MISMATCH,
    ),
]
MATRIX_IDS = [row[0] for row in MATRIX]
MATRIX_ARGS = ("case", "kind", "mode", "broker", "account", "code")


class _Stack:
    """A hand-built ledger, service and key. It does not use start_execution."""

    def __init__(self, tmp_path: Path, kind: str, double: RecordingDispatcher) -> None:
        self.ledger = ExecutionLedger(
            tmp_path / f"{kind}.sqlite3",
            workspace="uk",
            require_approval=True,
            venue=BINDING if kind == "practice" else None,
        )
        self.approval = ApprovalService(self.ledger)
        self.key = private_key()
        enroll(self.approval, self.key)
        self.double = double
        self.service = ExecutionService(
            double,
            self.ledger,
            require_approval=True,
            approval_service=self.approval,
        )

    def close(self) -> None:
        self.ledger.close()

    def register(self, intent: OrderIntent) -> None:
        self.ledger.register_intent(intent)

    def admit_and_reserve(self, intent: OrderIntent) -> None:
        self.service.admit(
            intent,
            currency="GBP",
            price="50",
            simulator_evidence={"simulated_fill_price": "50"},
            risk_evidence={"scaled_size": str(intent.quantity)},
        )
        self.ledger.configure_paper_budget(intent.account, "GBP", "1000", workspace="uk")
        self.service.reserve(intent.proposal_id)

    def forge_challenge(self, proposal_id: str) -> str:
        """Store a well-formed challenge directly, bypassing the approval layer.

        The ledger claim guard must hold even if the approval layer is skipped.
        """

        order = self.ledger.get_order(proposal_id)
        intent = dict(order.intent)
        key = self.ledger.get_approval_key(workspace="uk")
        issued = int(time.time())
        challenge_id = str(uuid.uuid4())
        payload = {
            "version": 1,
            "purpose": "growin.execution.dispatch",
            "challenge_id": challenge_id,
            "proposal_id": proposal_id,
            "client_order_id": order.client_order_id,
            "intent_hash": order.intent_hash,
            "workspace": intent["workspace"],
            "account": intent["account"],
            "broker": intent["broker"],
            "mode": intent["mode"],
            "ticker": intent["ticker"],
            "side": intent["side"],
            "quantity": intent["quantity"],
            "order_type": intent.get("order_type"),
            "limit_price": intent.get("limit_price"),
            "replaces_proposal_id": "",
            "requote_id": "",
            "nonce": "forged-nonce",
            "issued_at": issued,
            "expires_at": issued + 60,
            "key_id": key.key_id,
        }
        signed = canonical_json(payload).encode("utf-8")
        raw = sqlite3.connect(self.ledger.path)
        try:
            raw.execute(
                "INSERT INTO approval_challenges (challenge_id, proposal_id, workspace, key_id, "
                "intent_hash, signed_payload, issued_at_epoch, expires_at_epoch, created_at) "
                "VALUES (?, ?, 'uk', ?, ?, ?, ?, ?, 'forged')",
                (challenge_id, proposal_id, key.key_id, order.intent_hash, signed, issued, issued + 60),
            )
            raw.commit()
        finally:
            raw.close()
        self._signed = signed
        return challenge_id

    def signature(self) -> bytes:
        return sign(self.key, self._signed)


@pytest.fixture
def stack_factory(tmp_path):
    stacks: list[_Stack] = []

    def build(kind: str, double: RecordingDispatcher | None = None) -> _Stack:
        stack = _Stack(tmp_path, kind, double or RecordingDispatcher())
        stacks.append(stack)
        return stack

    yield build
    for stack in stacks:
        stack.close()


@pytest.mark.parametrize(MATRIX_ARGS, MATRIX, ids=MATRIX_IDS)
def test_challenge_accepts_only_the_mode_the_ledger_venue_allows(
    stack_factory, case, kind, mode, broker, account, code
):
    stack = stack_factory(kind)
    intent = _intent(f"m-{case}", mode=mode, broker=broker, account=account)
    stack.register(intent)
    if code is None:
        stack.admit_and_reserve(intent)
        challenge = stack.service.create_approval_challenge(intent.proposal_id, workspace="uk")
        assert json.loads(challenge.signed_payload)["mode"] == mode
        return
    with pytest.raises(ApprovalConflict) as refused:
        stack.service.create_approval_challenge(intent.proposal_id, workspace="uk")
    assert str(refused.value) == refusal_text(code)
    assert SYNTH_ACCOUNT not in str(refused.value)
    assert stack.ledger.list_attempts(intent.proposal_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(MATRIX_ARGS, MATRIX, ids=MATRIX_IDS)
async def test_claim_refuses_a_wrong_mode_even_with_a_valid_signature(
    stack_factory, case, kind, mode, broker, account, code
):
    """The ledger claim is the dispatch gate; it must not trust the approval layer."""

    stack = stack_factory(kind)
    intent = _intent(f"c-{case}", mode=mode, broker=broker, account=account)
    stack.register(intent)
    challenge_id = stack.forge_challenge(intent.proposal_id)
    signature = stack.signature()
    if code is None:
        # Allowed mode: the claim gets past the mode gate and stops at the
        # missing admission, which proves the gate did not refuse it.
        with pytest.raises(ApprovalConflict, match="admitted evidence"):
            stack.approval.approve_signed(
                intent.proposal_id, challenge_id, signature, workspace="uk"
            )
        return
    with pytest.raises(ApprovalConflict) as refused:
        stack.approval.approve_signed(
            intent.proposal_id, challenge_id, signature, workspace="uk"
        )
    assert str(refused.value) == refusal_text(code)
    assert stack.ledger.get_order(intent.proposal_id).state == "PENDING"
    assert stack.ledger.list_attempts(intent.proposal_id) == []
    assert stack.ledger.approval_evidence_count(intent.proposal_id) == 0
    assert stack.double.intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    MATRIX_ARGS, [row for row in MATRIX if row[5] is not None], ids=[r[0] for r in MATRIX if r[5]]
)
async def test_service_refuses_a_wrong_mode_before_any_dispatch(
    stack_factory, case, kind, mode, broker, account, code
):
    stack = stack_factory(kind)
    intent = _intent(f"s-{case}", mode=mode, broker=broker, account=account)
    stack.register(intent)
    with pytest.raises(ExecutionDisabledError) as refused:
        await stack.service.approve_signed(
            intent.proposal_id, str(uuid.uuid4()), b"signature", workspace="uk"
        )
    expected = "Live execution remains disabled" if code == LIVE_DISABLED else refusal_text(code)
    assert str(refused.value) == expected
    assert stack.double.intents == []


@pytest.mark.parametrize(MATRIX_ARGS, MATRIX, ids=MATRIX_IDS)
def test_unsigned_claim_path_applies_the_same_mode_rule(
    tmp_path, case, kind, mode, broker, account, code
):
    ledger = ExecutionLedger(
        tmp_path / "unsigned.sqlite3",
        workspace="uk",
        venue=BINDING if kind == "practice" else None,
    )
    try:
        intent = _intent(f"u-{case}", mode=mode, broker=broker, account=account)
        if code is None:
            assert ledger.claim_intent(intent).claimed
            return
        with pytest.raises(ApprovalConflict) as refused:
            ledger.claim_intent(intent)
        assert str(refused.value) == refusal_text(code)
        assert ledger.list_attempts(intent.proposal_id) == []
    finally:
        ledger.close()


def test_the_allowed_mode_follows_the_ledger_binding(stack_factory):
    assert stack_factory("paper").ledger.allowed_mode is OrderMode.PAPER
    assert stack_factory("practice").ledger.allowed_mode is OrderMode.PRACTICE


def test_live_is_in_no_venue_spec():
    from execution.venue import allowed_modes, registered_kinds, spec_for

    assert allowed_modes(None) == frozenset({"PAPER"})
    for kind in registered_kinds():
        assert "LIVE" not in spec_for(kind).modes


def test_practice_budget_is_only_for_the_bound_account(stack_factory):
    stack = stack_factory("practice")
    with pytest.raises(ApprovalConflict, match="bound account"):
        stack.ledger.configure_paper_budget("acct-other-0002", "GBP", "100", workspace="uk")
    with pytest.raises(ApprovalConflict, match="bound account"):
        stack.ledger.configure_paper_budget(SYNTH_ACCOUNT, "INR", "100", workspace="uk")
    assert stack.ledger.configure_paper_budget(
        SYNTH_ACCOUNT, "GBP", "100", workspace="uk"
    ).amount == Decimal("100")


def test_execution_mode_label_is_the_one_status_vocabulary():
    assert execution_mode_label(False, None) == "disabled"
    assert execution_mode_label(False, BINDING) == "disabled"
    assert execution_mode_label(True, None) == "paper"
    assert execution_mode_label(True, BINDING) == "practice"


# --- fail-closed venue selection at start (D-03, D-21, D-23) -------------------

SECRET_SENTINEL = "sentinel-config-value-7731"


class CountingFactory:
    """A registered factory that records how many dispatchers it built."""

    def __init__(self) -> None:
        self.double = RecordingDispatcher()
        self.calls = 0

    def __call__(self, context):
        self.calls += 1
        return self.double


def _factories(counting: CountingFactory) -> dict:
    return {**production_dispatcher_factories(), VENUE_T212_PRACTICE: counting}


def _assert_disabled(app_state: AppState, code: str) -> None:
    assert app_state.execution_authority is False
    assert app_state.execution_service.execution_enabled is False
    assert app_state._execution_ledger is None
    assert app_state.workspace_config is None
    assert app_state.execution_mode == "disabled"
    assert code in app_state.execution_startup_error
    assert SECRET_SENTINEL not in app_state.execution_startup_error
    assert SYNTH_ACCOUNT not in app_state.execution_startup_error


def test_no_execution_file_means_paper_on_the_default_ledger_exactly_as_before(
    tmp_path, private_config_dir, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    app_state = AppState()
    assert app_state.start_execution(None, workspace="uk", private_dir=private_config_dir)
    try:
        assert isinstance(app_state.execution_service._dispatcher, PaperDispatcher)
        assert app_state._execution_ledger.path == default_ledger_path("uk")
        assert app_state._execution_ledger.venue_binding is None
        assert app_state.execution_mode == "paper"
        assert app_state.workspace_config.venue == "paper"
    finally:
        app_state.close_execution()


def test_practice_venue_uses_its_own_ledger_path_never_the_real_one(
    tmp_path, private_config_dir, monkeypatch, uk_process
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_practice_files(private_config_dir)
    counting = CountingFactory()
    app_state = AppState()
    assert app_state.start_execution(
        None, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    ), app_state.execution_startup_error
    try:
        assert app_state._execution_ledger.path == practice_ledger_path()
        assert app_state._execution_ledger.path != default_ledger_path("uk")
        assert not default_ledger_path("uk").exists()
        assert counting.calls == 1
    finally:
        app_state.close_execution()


def test_factory_receives_the_binding_and_caps_without_leaking_them_in_repr(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    seen = []

    def factory(context):
        seen.append(context)
        return RecordingDispatcher()

    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "p.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories={**production_dispatcher_factories(), VENUE_T212_PRACTICE: factory},
    )
    try:
        (context,) = seen
        assert context.venue == VENUE_T212_PRACTICE
        assert context.binding.account_id == SYNTH_ACCOUNT
        assert context.caps.capital_cap == Decimal("900.00")
        assert context.caps.per_position_cap == Decimal("300.00")
        assert "900" not in repr(context) and SYNTH_ACCOUNT not in repr(context)
    finally:
        app_state.close_execution()


def test_practice_budget_equals_capital_cap_and_stays_immutable(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"
    counting = CountingFactory()
    first = AppState()
    assert first.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    try:
        budget = first._execution_ledger.get_paper_budget(SYNTH_ACCOUNT, "GBP", workspace="uk")
        assert budget.amount == Decimal("900.00")
    finally:
        first.close_execution()

    # Same ledger, same caps: starts again.
    again = AppState()
    assert again.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    again.close_execution()

    # Changed caps: the budget is immutable, so start fails closed.
    write_json(
        private_config_dir / "uk" / "limits.json", {**SYNTH_LIMITS, "capital_cap": "901.00"}
    )
    changed = AppState()
    assert changed.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    ) is False
    _assert_disabled(changed, "ApprovalConflict")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"venue": "t212_live"}, "VENUE_UNKNOWN"),
        ({"venue": SECRET_SENTINEL}, "VENUE_UNKNOWN"),
        ({"surprise": SECRET_SENTINEL}, "SCHEMA_INVALID"),
        ({"account_id": 1.5}, "FLOAT_NOT_ALLOWED"),
        ({"currency": "USD"}, "CURRENCY_MISMATCH"),
        ({"workspace": "india"}, "WORKSPACE_MISMATCH"),
    ],
)
def test_invalid_execution_file_disables_execution_and_never_falls_back_to_paper(
    tmp_path, private_config_dir, uk_process, overrides, code
):
    write_practice_files(private_config_dir, execution=practice_execution_payload(**overrides))
    counting = CountingFactory()
    ledger_dir = tmp_path / "ledger"
    app_state = AppState()

    started = app_state.start_execution(
        ledger_dir / "execution.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, code)
    assert not ledger_dir.exists()
    assert counting.calls == 0


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_open_permissions_on_execution_json_disable_execution(
    tmp_path, private_config_dir, uk_process, mode
):
    write_practice_files(private_config_dir)
    os.chmod(private_config_dir / "uk" / "execution.json", mode)
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "x.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    ) is False
    _assert_disabled(app_state, "PERMISSIONS_TOO_OPEN")


def test_a_symlinked_execution_json_disables_execution(tmp_path, private_config_dir, uk_process):
    write_practice_files(private_config_dir)
    link = private_config_dir / "uk" / "execution.json"
    real = private_config_dir / "uk" / "elsewhere.json"
    link.rename(real)
    link.symlink_to(real)
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "x.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    ) is False
    _assert_disabled(app_state, "SYMLINK_REFUSED")


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        (None, "FILE_MISSING"),
        ({"capital_cap": 900}, "SCHEMA_INVALID"),
        ({"capital_cap": 900.5}, "FLOAT_NOT_ALLOWED"),
        ({"per_position_cap": "901.00"}, "LIMIT_ORDER_INVALID"),
        ({"per_position_cap": "0"}, "LIMIT_ORDER_INVALID"),
    ],
)
def test_missing_or_invalid_limits_disable_the_practice_venue(
    tmp_path, private_config_dir, uk_process, limits, code
):
    write_practice_files(private_config_dir)
    limits_path = private_config_dir / "uk" / "limits.json"
    if limits is None:
        limits_path.unlink()
    else:
        write_json(limits_path, {**SYNTH_LIMITS, **limits})
    counting = CountingFactory()
    ledger_dir = tmp_path / "ledger"
    app_state = AppState()

    started = app_state.start_execution(
        ledger_dir / "execution.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, code)
    assert not ledger_dir.exists()
    assert counting.calls == 0


def test_practice_venue_with_no_registered_factory_is_disabled_never_paper(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    ledger_dir = tmp_path / "ledger"
    app_state = AppState()

    # No dispatcher_factories: the production map, which holds paper only.
    started = app_state.start_execution(
        ledger_dir / "execution.sqlite3", workspace="uk", private_dir=private_config_dir
    )

    assert started is False
    _assert_disabled(app_state, "VENUE_UNAVAILABLE")
    assert not ledger_dir.exists()
    assert not isinstance(app_state.execution_service._dispatcher, PaperDispatcher)


@pytest.mark.parametrize("process", [None, "india", "us", "UK"])
def test_practice_venue_cannot_start_outside_a_uk_process(
    tmp_path, private_config_dir, monkeypatch, process
):
    if process is None:
        monkeypatch.delenv("GROWIN_WORKSPACE", raising=False)
    else:
        monkeypatch.setenv("GROWIN_WORKSPACE", process)
    write_practice_files(private_config_dir)
    counting = CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        tmp_path / "x.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, "VENUE_WORKSPACE_MISMATCH")
    assert counting.calls == 0
    assert not (tmp_path / "x.sqlite3").exists()


def test_an_india_execution_file_naming_trading212_cannot_start(
    tmp_path, private_config_dir, monkeypatch
):
    monkeypatch.setenv("GROWIN_WORKSPACE", "india")
    write_json(
        private_config_dir / "india" / "execution.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "venue": VENUE_T212_PRACTICE,
            "account_id": SYNTH_ACCOUNT,
            "currency": "GBP",
        },
    )
    counting = CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        tmp_path / "india.sqlite3", workspace="india", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, "VENUE_NOT_ALLOWED")
    assert counting.calls == 0
    assert not (tmp_path / "india.sqlite3").exists()


def test_india_paper_execution_file_starts_on_paper(tmp_path, private_config_dir):
    write_json(
        private_config_dir / "india" / "execution.json",
        {"schema_version": 1, "workspace": "india", "venue": "paper"},
    )
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "india.sqlite3", workspace="india", private_dir=private_config_dir
    )
    try:
        assert isinstance(app_state.execution_service._dispatcher, PaperDispatcher)
        assert app_state.execution_mode == "paper"
    finally:
        app_state.close_execution()


def test_practice_ledger_reopened_with_another_account_id_cannot_start(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"
    counting = CountingFactory()
    first = AppState()
    assert first.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    first.close_execution()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    write_practice_files(
        private_config_dir, execution=practice_execution_payload(account_id="acct-other-0002")
    )
    second = AppState()

    started = second.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(second, "LedgerVenueMismatch")
    assert "acct-other-0002" not in second.execution_startup_error
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_the_real_uk_ledger_is_never_started_as_practice(
    tmp_path, private_config_dir, uk_process
):
    real = tmp_path / "real-uk.sqlite3"
    with ExecutionLedger(real, workspace="uk") as ledger:
        ledger.register_intent(
            _intent("real-1", mode="PAPER", broker="paper", account="invest")
        )
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    write_practice_files(private_config_dir)
    counting = CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        real, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, "LedgerVenueMismatch")
    assert counting.calls == 0
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before


def test_a_practice_ledger_is_never_started_as_paper(tmp_path, private_config_dir, uk_process):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"
    first = AppState()
    assert first.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    )
    first.close_execution()
    (private_config_dir / "uk" / "execution.json").unlink()
    second = AppState()

    started = second.start_execution(path, workspace="uk", private_dir=private_config_dir)

    assert started is False
    _assert_disabled(second, "LedgerVenueMismatch")


def test_a_failing_factory_leaves_execution_disabled_and_releases_the_ledger(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"

    def broken(_context):
        raise VenueError("VENUE_BUILD_FAILED", VENUE_T212_PRACTICE)

    app_state = AppState()
    started = app_state.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories={**production_dispatcher_factories(), VENUE_T212_PRACTICE: broken},
    )
    assert started is False
    _assert_disabled(app_state, "VENUE_BUILD_FAILED")
    # The writer lock was released, so the same file opens again.
    again = AppState()
    assert again.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    )
    again.close_execution()


def test_uat_builders_refuse_a_practice_ledger(tmp_path, private_config_dir, uk_process):
    write_practice_files(private_config_dir)
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "p.sqlite3", workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    )
    try:
        with pytest.raises(Exception, match="unavailable in a practice ledger"):
            app_state.create_paper_approval_check()
        with pytest.raises(Exception, match="unavailable in a practice ledger"):
            app_state.create_paper_requote_check()
    finally:
        app_state.close_execution()


# --- one shared mode function across the three status surfaces ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("venue_kind", ["practice", "paper", "disabled"])
async def test_status_surfaces_report_the_same_mode(
    tmp_path, private_config_dir, monkeypatch, venue_kind
):
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from app_context import state
    from server import app

    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setattr(state, "_chat_manager", MagicMock())
    state.close_execution()
    try:
        if venue_kind == "practice":
            write_practice_files(private_config_dir)
            assert state.start_execution(
                tmp_path / "p.sqlite3", workspace="uk", private_dir=private_config_dir,
                dispatcher_factories=_factories(CountingFactory()),
            )
        elif venue_kind == "paper":
            assert state.start_execution(
                tmp_path / "x.sqlite3", workspace="uk", private_dir=private_config_dir
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            system = (await client.get("/api/system/status")).json()["execution"]["mode"]
            health = (await client.get("/health")).json()["execution_mode"]
            approval = (
                await client.get("/api/ai/trade/approval/status", params={"workspace": "uk"})
            ).json()["mode"]
        assert system == health == approval == venue_kind == state.execution_mode
    finally:
        state.close_execution()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", ["paper-uat", "paper-uat-v2"])
async def test_a_practice_ack_is_never_settled_as_a_local_uat_cancellation(
    tmp_path, private_config_dir, monkeypatch, account_id
):
    """A practice account named like the UAT account keeps its real ACK."""

    import base64
    from unittest.mock import MagicMock

    from httpx import ASGITransport, AsyncClient

    from app_context import state
    from server import app

    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setattr(state, "_chat_manager", MagicMock())
    write_practice_files(
        private_config_dir, execution=practice_execution_payload(account_id=account_id)
    )
    double = RecordingDispatcher()
    state.close_execution()
    try:
        assert state.start_execution(
            tmp_path / "p.sqlite3", workspace="uk", private_dir=private_config_dir,
            dispatcher_factories=practice_factories(double),
        ), state.execution_startup_error
        key = private_key()
        enroll(state.execution_service._approval_service, key)
        proposal = practice_proposal(account=account_id)
        prepare(state, proposal)
        pid = proposal["proposal_id"]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            challenge = (
                await client.post(
                    "/api/ai/trade/approval/challenge",
                    json={"proposal_id": pid, "workspace": "uk"},
                )
            ).json()
            signature = sign(key, base64.b64decode(challenge["signed_payload_b64"]))
            response = await client.post(
                "/api/ai/trade/approval/complete",
                json={
                    "proposal_id": pid,
                    "challenge_id": challenge["challenge_id"],
                    "signature_der_b64": base64.b64encode(signature).decode("ascii"),
                    "workspace": "uk",
                },
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "released" not in body["message"] and "No broker was contacted" not in body["message"]
        assert body["execution_details"]["broker"] == VENUE_T212_PRACTICE
        assert len(double.intents) == 1
        order = state._execution_ledger.get_order(pid)
        assert order.state == "ACKNOWLEDGED"
        budget = state._execution_ledger.get_paper_budget(account_id, "GBP", workspace="uk")
        assert budget.reserved == Decimal("100") and budget.released == Decimal("0")
    finally:
        state.close_execution()


def test_no_trading_212_host_string_in_execution_or_app_context():
    root = Path(__file__).resolve().parents[2] / "backend"
    hits = []
    for path in [*(root / "execution").rglob("*.py"), root / "app_context.py"]:
        text = path.read_text(encoding="utf-8")
        if "trading212.com" in text:
            hits.append(str(path.relative_to(root)))
    assert hits == []


# --- the LIMIT-as-market dispatcher is gone (D-10c) ------------------------------

import ast

ORDER_TOOL_NAMES = (
    "place_market_order",
    "place_limit_order",
    "place_stop_order",
    "place_stop_limit_order",
    "cancel_order",
    "create_investment_pie",
    "update_investment_pie",
    "delete_investment_pie",
    "update_pie",
    "switch_account",
)
BACKEND = Path(__file__).resolve().parents[2] / "backend"


def _order_tool_offences(paths) -> list[str]:
    """Every call_tool call and every order or cancel tool name in these files."""

    offences = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "call_tool":
                    offences.append(f"{Path(path).name}:{node.lineno} call_tool")
            if isinstance(node, ast.Constant) and node.value in ORDER_TOOL_NAMES:
                offences.append(f"{Path(path).name}:{node.lineno} {node.value}")
    return offences


def test_trading212_dispatcher_is_gone():
    with pytest.raises(ImportError):
        from execution import Trading212Dispatcher  # noqa: F401
    with pytest.raises(ImportError):
        import execution.t212_dispatcher  # noqa: F401
    import execution

    assert not hasattr(execution, "Trading212Dispatcher")
    assert "Trading212Dispatcher" not in execution.__all__
    assert not (BACKEND / "execution" / "t212_dispatcher.py").exists()


def test_no_execution_or_app_context_code_calls_an_mcp_order_tool():
    sources = [*(BACKEND / "execution").rglob("*.py"), BACKEND / "app_context.py"]
    assert len(sources) > 5
    assert _order_tool_offences(sources) == []


def test_the_order_tool_scan_catches_a_planted_call(tmp_path):
    """The scan above is only evidence if it can fail."""

    planted = tmp_path / "planted.py"
    planted.write_text(
        "async def go(client):\n"
        "    await client.call_tool('place_market_order', {'ticker': 'X'})\n",
        encoding="utf-8",
    )
    assert _order_tool_offences([planted]) == [
        "planted.py:2 call_tool",
        "planted.py:2 place_market_order",
    ]


# --- a practice ledger never touches the real UK ledger path (D-01) -------------


def _real_uk_ledger_traces(real: Path) -> list[str]:
    """Everything the real path could leave behind: file, lock, parent directory."""

    names = [real, real.with_name(real.name + ".lock"), real.parent]
    return [str(path) for path in names if os.path.lexists(path)]


@pytest.fixture
def home_with_real_uk_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return default_ledger_path("uk")


def test_a_practice_binding_with_no_path_opens_the_practice_ledger_not_the_real_one(
    home_with_real_uk_path,
):
    real = home_with_real_uk_path
    with ExecutionLedger(workspace="uk", venue=BINDING) as ledger:
        assert ledger.path == practice_ledger_path()
        assert ledger.path != real
        assert ledger.venue_binding == BINDING
    assert _real_uk_ledger_traces(real) == []


@pytest.mark.parametrize("state", ["absent", "empty"])
def test_a_practice_binding_refuses_the_real_uk_path_before_touching_it(
    home_with_real_uk_path, state
):
    real = home_with_real_uk_path
    if state == "empty":
        real.parent.mkdir(parents=True)
        real.write_bytes(b"")
        before = (real.stat().st_size, real.stat().st_mtime_ns)
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(real, workspace="uk", venue=BINDING)
    if state == "absent":
        assert _real_uk_ledger_traces(real) == []
    else:
        assert (real.stat().st_size, real.stat().st_mtime_ns) == before
        assert real.read_bytes() == b""
        assert not real.with_name(real.name + ".lock").exists()


def test_a_practice_binding_refuses_aliases_of_the_real_uk_path(
    home_with_real_uk_path, tmp_path
):
    real = home_with_real_uk_path
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(real)
    dotted = real.parent / ".." / real.parent.name / real.name
    # macOS file systems are case-insensitive: a recased name can be the real file.
    recased = real.with_name(real.name.upper())
    for alias in (link, dotted, recased):
        with pytest.raises(LedgerVenueMismatch):
            ExecutionLedger(alias, workspace="uk", venue=BINDING)
    assert _real_uk_ledger_traces(real) == []
    assert not link.with_name(link.name + ".lock").exists()


@pytest.mark.parametrize("state", ["absent", "empty"])
def test_start_execution_never_turns_the_real_uk_path_into_a_practice_ledger(
    tmp_path, private_config_dir, uk_process, home_with_real_uk_path, state
):
    real = home_with_real_uk_path
    if state == "empty":
        real.parent.mkdir(parents=True)
        real.write_bytes(b"")
    write_practice_files(private_config_dir)
    counting = CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        real, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(app_state, "LedgerVenueMismatch")
    assert counting.calls == 0
    if state == "absent":
        assert _real_uk_ledger_traces(real) == []
    else:
        assert real.read_bytes() == b""
        assert not real.with_name(real.name + ".lock").exists()
    # The next paper startup still owns that path: it pins it as a paper ledger.
    (private_config_dir / "uk" / "execution.json").unlink()
    (private_config_dir / "uk" / "limits.json").unlink()
    paper = AppState()
    assert paper.start_execution(None, workspace="uk", private_dir=private_config_dir)
    try:
        assert paper._execution_ledger.path == real
        assert paper._execution_ledger.venue_binding is None
    finally:
        paper.close_execution()


def _stored_limits(path: Path) -> tuple:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT capital_cap, per_position_cap FROM ledger_venue_limits"
        ).fetchall()
    finally:
        connection.close()


def test_both_practice_caps_are_stored_with_the_ledger_and_cannot_be_rewritten(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"
    app_state = AppState()
    assert app_state.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    )
    app_state.close_execution()

    assert _stored_limits(path) == [("900.00", "300.00")]
    raw = sqlite3.connect(path)
    try:
        for statement in (
            "UPDATE ledger_venue_limits SET per_position_cap = '999'",
            "DELETE FROM ledger_venue_limits",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                raw.execute(statement)
    finally:
        raw.close()


def test_changing_only_the_per_position_cap_cannot_restart_a_practice_ledger(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    path = tmp_path / "p.sqlite3"
    counting = CountingFactory()
    first = AppState()
    assert first.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    first.close_execution()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    write_json(
        private_config_dir / "uk" / "limits.json", {**SYNTH_LIMITS, "per_position_cap": "299.00"}
    )
    changed = AppState()
    started = changed.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    _assert_disabled(changed, "ApprovalConflict")
    assert counting.calls == 1
    assert _stored_limits(path) == [("900.00", "300.00")]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    # The unchanged caps still start it.
    write_json(private_config_dir / "uk" / "limits.json", dict(SYNTH_LIMITS))
    again = AppState()
    assert again.start_execution(
        path, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    again.close_execution()


def test_a_changed_cap_pair_writes_nothing(stack_factory):
    stack = stack_factory("practice")
    first = stack.ledger.configure_venue_limits("900", "300", workspace="uk")
    assert first.amount == Decimal("900")
    for capital, per_position in (("900", "299"), ("901", "300"), ("901", "299"), ("950", "300")):
        with pytest.raises(ApprovalConflict, match="immutable"):
            stack.ledger.configure_venue_limits(capital, per_position, workspace="uk")
    assert _stored_limits(stack.ledger.path) == [("900", "300")]
    assert stack.ledger.get_paper_budget(SYNTH_ACCOUNT, "GBP", workspace="uk").amount == Decimal("900")
    # The same pair, in any decimal spelling, is accepted.
    assert stack.ledger.configure_venue_limits("900.00", "300.0", workspace="uk").amount == Decimal("900")


def test_venue_limits_exist_only_in_a_bound_ledger_and_stay_ordered(stack_factory):
    paper = stack_factory("paper")
    with pytest.raises(ApprovalConflict, match="bound-venue"):
        paper.ledger.configure_venue_limits("900", "300", workspace="uk")
    tables = {
        row[0]
        for row in sqlite3.connect(paper.ledger.path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "ledger_venue_limits" not in tables
    practice = stack_factory("practice")
    with pytest.raises(ApprovalConflict, match="exceeds"):
        practice.ledger.configure_venue_limits("300", "900", workspace="uk")
    assert practice.ledger.get_paper_budget(SYNTH_ACCOUNT, "GBP", workspace="uk") is None


def test_start_execution_with_no_path_uses_the_practice_path_and_leaves_the_real_one_absent(
    private_config_dir, uk_process, home_with_real_uk_path
):
    real = home_with_real_uk_path
    write_practice_files(private_config_dir)
    app_state = AppState()
    assert app_state.start_execution(
        None, workspace="uk", private_dir=private_config_dir,
        dispatcher_factories=_factories(CountingFactory()),
    ), app_state.execution_startup_error
    try:
        assert app_state._execution_ledger.path == practice_ledger_path()
    finally:
        app_state.close_execution()
    assert _real_uk_ledger_traces(real) == []
