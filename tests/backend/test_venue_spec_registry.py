"""Phase 66-01 review fix: the venue binding rule is data, not code.

One ``VenueSpec`` per venue kind carries workspace, currency, order modes, the
default ledger path and the dispatcher key. These tests register a fake second
spec (india, INR, SHADOW only) in a test-only registry and prove that the guards
apply it with no guard code changed: they never name a venue kind.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import venue_registry
from app_context import AppState
from execution import ApprovalConflict, ExecutionLedger, default_ledger_path
from execution.ledger import (
    LedgerVenueMismatch,
    _install_venue_binding,
    _venue_binding_check,
)
from execution.venue import (
    ACCOUNT_BINDING_MISMATCH,
    BROKER_VENUE_MISMATCH,
    LIVE_DISABLED,
    MODE_VENUE_MISMATCH,
    VENUE_T212_PRACTICE,
    VenueBinding,
    VenueError,
    allowed_modes,
    intent_refusal,
    production_dispatcher_factories,
    resolve_factory,
)
from private_config import PrivateConfigError, load_workspace_config
from venue_registry import VenueSpec
from venue_seam_testkit import write_json

FAKE_KIND = "fake_india_shadow"
FAKE_ACCOUNT = "acct-fake-india-0001"
# Deliberately not the kind: the factory lookup must follow the spec.
FAKE_KEY = "fake_shadow_factory"


def _fake_spec(tmp_path: Path) -> VenueSpec:
    return VenueSpec(
        kind=FAKE_KIND,
        workspace="india",
        currency="INR",
        modes=frozenset({"SHADOW"}),
        ledger_path=lambda: tmp_path / "fake-india" / "execution.sqlite3",
        dispatcher_key=FAKE_KEY,
    )


@pytest.fixture
def with_fake_spec(monkeypatch, tmp_path):
    """The production registry plus the fake spec, for this test only."""

    spec = _fake_spec(tmp_path)
    monkeypatch.setattr(
        venue_registry,
        "VENUE_SPECS",
        {**venue_registry.VENUE_SPECS, FAKE_KIND: spec},
    )
    return spec


def _fake_binding() -> VenueBinding:
    return VenueBinding(venue=FAKE_KIND, account_id=FAKE_ACCOUNT, currency="INR")


def test_production_registry_holds_only_the_practice_spec():
    assert venue_registry.registered_kinds() == (VENUE_T212_PRACTICE,)
    spec = venue_registry.spec_for(VENUE_T212_PRACTICE)
    assert (spec.workspace, spec.currency, spec.modes) == ("uk", "GBP", frozenset({"PRACTICE"}))
    assert spec.dispatcher_key == VENUE_T212_PRACTICE
    assert venue_registry.spec_for("breeze_relay") is None


def test_the_fake_spec_binding_takes_its_own_currency(with_fake_spec):
    assert _fake_binding().venue == FAKE_KIND
    with pytest.raises(ValueError):
        VenueBinding(venue=FAKE_KIND, account_id=FAKE_ACCOUNT, currency="GBP")
    with pytest.raises(ValueError):
        VenueBinding(venue=VENUE_T212_PRACTICE, account_id=FAKE_ACCOUNT, currency="INR")


def test_fake_spec_intents_in_other_modes_are_refused(with_fake_spec):
    binding = _fake_binding()
    assert allowed_modes(binding) == frozenset({"SHADOW"})
    assert intent_refusal("SHADOW", FAKE_KIND, FAKE_ACCOUNT, binding) is None
    for other in ("PAPER", "PRACTICE", "shadowx", ""):
        assert intent_refusal(other, FAKE_KIND, FAKE_ACCOUNT, binding) == MODE_VENUE_MISMATCH
    assert intent_refusal("SHADOW", "paper", FAKE_ACCOUNT, binding) == BROKER_VENUE_MISMATCH
    assert intent_refusal("SHADOW", FAKE_KIND, "acct-other", binding) == ACCOUNT_BINDING_MISMATCH


@pytest.mark.parametrize("mode", ["LIVE", "live", "Live"])
def test_live_is_refused_for_every_spec(with_fake_spec, mode):
    assert intent_refusal(mode, FAKE_KIND, FAKE_ACCOUNT, _fake_binding()) == LIVE_DISABLED
    practice = VenueBinding(venue=VENUE_T212_PRACTICE, account_id=FAKE_ACCOUNT, currency="GBP")
    assert intent_refusal(mode, VENUE_T212_PRACTICE, FAKE_ACCOUNT, practice) == LIVE_DISABLED
    assert intent_refusal(mode, "paper", "invest", None) == LIVE_DISABLED


@pytest.mark.parametrize(
    "modes", [{"LIVE"}, {"PRACTICE", "LIVE"}, {"live"}, set(), {"lower"}]
)
def test_no_spec_can_be_built_that_allows_live_or_nothing(tmp_path, modes):
    with pytest.raises(ValueError):
        VenueSpec(
            kind="bad_kind",
            workspace="india",
            currency="INR",
            modes=frozenset(modes),
            ledger_path=lambda: tmp_path / "x.sqlite3",
            dispatcher_key="bad_kind",
        )


@pytest.mark.parametrize("kind", ["t212-practice", "T212_PRACTICE", "paper", "", "two__under"])
def test_venue_kinds_are_underscore_names(tmp_path, kind):
    with pytest.raises(ValueError):
        VenueSpec(
            kind=kind,
            workspace="uk",
            currency="GBP",
            modes=frozenset({"PRACTICE"}),
            ledger_path=lambda: tmp_path / "x.sqlite3",
            dispatcher_key="t212_practice",
        )


def test_the_fake_spec_cannot_open_a_uk_ledger(with_fake_spec, tmp_path):
    path = tmp_path / "uk-fake.sqlite3"
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="uk", venue=_fake_binding())
    assert not path.exists()
    assert not path.with_name(path.name + ".lock").exists()


def test_the_practice_spec_cannot_open_an_india_ledger(tmp_path):
    binding = VenueBinding(venue=VENUE_T212_PRACTICE, account_id=FAKE_ACCOUNT, currency="GBP")
    path = tmp_path / "india-practice.sqlite3"
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="india", venue=binding)
    assert not path.exists()


def test_the_fake_spec_opens_its_own_workspace_ledger_and_applies_its_modes(
    with_fake_spec, tmp_path
):
    binding = _fake_binding()
    with ExecutionLedger(workspace="india", venue=binding) as ledger:
        # No path given: the spec's default ledger path function decides.
        assert ledger.path == with_fake_spec.ledger_path()
        assert ledger.allowed_modes == frozenset({"SHADOW"})
        ledger._require_intent_allowed(
            {"mode": "SHADOW", "broker": FAKE_KIND, "account": FAKE_ACCOUNT}
        )
        for other in ("PAPER", "PRACTICE", "LIVE"):
            with pytest.raises(ApprovalConflict):
                ledger._require_intent_allowed(
                    {"mode": other, "broker": FAKE_KIND, "account": FAKE_ACCOUNT}
                )
    # The same binding reopens; a paper open of that file is refused.
    with ExecutionLedger(workspace="india", venue=binding):
        pass
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(with_fake_spec.ledger_path(), workspace="india")


def test_an_unbound_ledger_accepts_paper_only_so_the_fake_mode_is_refused(
    with_fake_spec, tmp_path
):
    assert allowed_modes(None) == frozenset({"PAPER"})
    assert intent_refusal("SHADOW", FAKE_KIND, FAKE_ACCOUNT, None) == MODE_VENUE_MISMATCH
    with ExecutionLedger(tmp_path / "paper.sqlite3", workspace="india") as ledger:
        with pytest.raises(ApprovalConflict):
            ledger._require_intent_allowed(
                {"mode": "SHADOW", "broker": FAKE_KIND, "account": FAKE_ACCOUNT}
            )


def test_an_unregistered_venue_fails_closed_everywhere(monkeypatch, tmp_path):
    with monkeypatch.context() as scope:
        scope.setattr(
            venue_registry,
            "VENUE_SPECS",
            {**venue_registry.VENUE_SPECS, FAKE_KIND: _fake_spec(tmp_path)},
        )
        binding = _fake_binding()
        resolve_factory(FAKE_KIND, {FAKE_KEY: lambda _ctx: None})
    # Registry back to production: the same binding value is now unregistered.
    assert venue_registry.spec_for(FAKE_KIND) is None
    assert allowed_modes(binding) == frozenset()
    assert intent_refusal("SHADOW", FAKE_KIND, FAKE_ACCOUNT, binding) == MODE_VENUE_MISMATCH
    assert intent_refusal("PRACTICE", FAKE_KIND, FAKE_ACCOUNT, binding) == MODE_VENUE_MISMATCH
    with pytest.raises(VenueError) as refused:
        resolve_factory(FAKE_KIND, {FAKE_KEY: lambda _ctx: None})
    assert refused.value.code == "VENUE_UNKNOWN"
    with pytest.raises(ValueError):
        VenueBinding(venue=FAKE_KIND, account_id=FAKE_ACCOUNT, currency="INR")
    path = tmp_path / "unregistered.sqlite3"
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="india", venue=binding)
    assert not path.exists()


def test_a_registered_venue_without_a_factory_is_unavailable_never_paper(with_fake_spec):
    with pytest.raises(VenueError) as refused:
        resolve_factory(FAKE_KIND, {"paper": lambda _ctx: None})
    assert refused.value.code == "VENUE_UNAVAILABLE"
    with pytest.raises(VenueError):
        resolve_factory(FAKE_KIND, {FAKE_KIND: lambda _ctx: None})
    sentinel = object()
    assert resolve_factory(FAKE_KIND, {FAKE_KEY: sentinel}) is sentinel


def test_the_binding_ddl_is_enumerated_from_the_registered_specs(with_fake_spec):
    check = _venue_binding_check()
    assert f"venue = '{FAKE_KIND}' AND currency = 'INR'" in check
    assert f"venue = '{VENUE_T212_PRACTICE}' AND currency = 'GBP'" in check

    connection = sqlite3.connect(":memory:")
    _install_venue_binding(connection, _fake_binding(), "now")
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'ledger_venue_binding'"
    ).fetchone()[0]
    connection.close()

    probe = sqlite3.connect(":memory:")
    probe.execute(ddl.replace("ledger_venue_binding", "probe_binding"))

    def insert(venue: str, currency: str) -> None:
        probe.execute(
            "INSERT OR REPLACE INTO probe_binding VALUES (1, ?, 'acct', ?, 'now')",
            (venue, currency),
        )

    insert(FAKE_KIND, "INR")
    insert(VENUE_T212_PRACTICE, "GBP")
    for venue, currency in (
        (FAKE_KIND, "GBP"),
        (VENUE_T212_PRACTICE, "INR"),
        ("breeze_relay", "INR"),
        ("paper", "GBP"),
        ("", "INR"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            insert(venue, currency)
    probe.close()


def test_the_production_binding_ddl_allows_only_the_practice_pair():
    assert _venue_binding_check() == (
        f"(venue = '{VENUE_T212_PRACTICE}' AND currency = 'GBP')"
    )


def test_a_bound_venue_never_opens_the_workspace_real_ledger(
    with_fake_spec, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    real = default_ledger_path("india")
    for path in (real, None):
        monkeypatch.setattr(
            venue_registry,
            "VENUE_SPECS",
            {
                **venue_registry.VENUE_SPECS,
                FAKE_KIND: VenueSpec(
                    kind=FAKE_KIND,
                    workspace="india",
                    currency="INR",
                    modes=frozenset({"SHADOW"}),
                    # A badly declared spec: its default path IS the real ledger.
                    ledger_path=lambda: real,
                    dispatcher_key=FAKE_KEY,
                ),
            },
        )
        with pytest.raises(LedgerVenueMismatch):
            ExecutionLedger(path, workspace="india", venue=_fake_binding())
        assert not real.exists() and not real.parent.exists()


def _fake_execution_payload(workspace: str) -> dict:
    return {
        "schema_version": 1,
        "workspace": workspace,
        "venue": FAKE_KIND,
        "account_id": FAKE_ACCOUNT,
        "currency": "INR",
    }


def test_the_loader_applies_the_fake_spec_workspace(with_fake_spec, private_config_dir):
    write_json(private_config_dir / "india" / "execution.json", _fake_execution_payload("india"))
    config = load_workspace_config(private_config_dir, "india")
    assert config.venue == FAKE_KIND
    assert config.execution.account_id == FAKE_ACCOUNT
    assert config.uk_limits is None

    # The same venue named in a uk workspace is refused, and so is a practice
    # venue named in india: both read the specs' workspaces.
    write_json(
        private_config_dir / "uk" / "execution.json",
        {**_fake_execution_payload("uk"), "currency": "GBP"},
    )
    with pytest.raises(PrivateConfigError) as refused:
        load_workspace_config(private_config_dir, "uk")
    assert refused.value.code == "VENUE_NOT_ALLOWED"
    write_json(
        private_config_dir / "india" / "execution.json",
        {**_fake_execution_payload("india"), "venue": VENUE_T212_PRACTICE, "currency": "GBP"},
    )
    with pytest.raises(PrivateConfigError):
        load_workspace_config(private_config_dir, "india")


def test_the_loader_refuses_the_fake_spec_currency_mismatch(with_fake_spec, private_config_dir):
    write_json(
        private_config_dir / "india" / "execution.json",
        {**_fake_execution_payload("india"), "currency": "GBP"},
    )
    with pytest.raises(PrivateConfigError):
        load_workspace_config(private_config_dir, "india")


def test_an_unregistered_venue_in_execution_json_is_unknown(private_config_dir):
    write_json(private_config_dir / "india" / "execution.json", _fake_execution_payload("india"))
    with pytest.raises(PrivateConfigError) as refused:
        load_workspace_config(private_config_dir, "india")
    assert refused.value.code == "VENUE_UNKNOWN"


def test_start_execution_uses_the_fake_spec_path_factory_and_process_workspace(
    with_fake_spec, private_config_dir, monkeypatch
):
    write_json(private_config_dir / "india" / "execution.json", _fake_execution_payload("india"))
    built = []

    def factory(context):
        built.append(context)
        return object()

    factories = {**production_dispatcher_factories(), FAKE_KEY: factory}

    # A uk process may not start an india-bound venue.
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    refused = AppState()
    assert not refused.start_execution(
        None, workspace="india", private_dir=private_config_dir, dispatcher_factories=factories
    )
    assert "VENUE_WORKSPACE_MISMATCH" in refused.execution_startup_error
    assert not built and not with_fake_spec.ledger_path().exists()

    monkeypatch.setenv("GROWIN_WORKSPACE", "india")
    app_state = AppState()
    assert app_state.start_execution(
        None, workspace="india", private_dir=private_config_dir, dispatcher_factories=factories
    ), app_state.execution_startup_error
    try:
        assert app_state._execution_ledger.path == with_fake_spec.ledger_path()
        assert app_state._execution_ledger.venue_binding.venue == FAKE_KIND
        (context,) = built
        assert context.binding.currency == "INR" and context.caps is None
    finally:
        app_state.close_execution()


GUARD_FILES = (
    "backend/execution/venue.py",
    "backend/execution/ledger.py",
    "backend/execution/approval.py",
    "backend/execution/service.py",
    "backend/app_context.py",
    "backend/private_config/loader.py",
    "backend/private_config/schemas.py",
)


def test_no_guard_source_names_a_venue_kind():
    """The guards read specs. Only the registry names a kind."""

    root = Path(__file__).resolve().parents[2]
    offences = []
    for relative in GUARD_FILES:
        text = (root / relative).read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            code = line.split("#", 1)[0]
            if re.search(r"""['"]t212[_-]|['"]breeze""", code):
                offences.append(f"{relative}:{number}: venue kind literal")
    assert not offences, offences
