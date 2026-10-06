"""Review round 2: nothing that can be checked before the holdout opens may fail after it.

Policy values, the ETF benchmark's presence and prices, the private spent-holdouts ledger, event/tag
cross-checks, and a typed INVALID record for failures that cannot be known in advance.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import date
from decimal import Decimal

import pytest

from costs.core import InputError

from strategy_india import __main__ as cli
from strategy_india import data, study
from strategy_india.data import DividendEvents
from strategy_india.errors import HoldoutInvalid, HoldoutSpent, RegistryError, StrategyIndiaError
from strategy_india.holdout import check_supported, parse_criteria

from test_strategy_india_support import (
    ETF_ISINS,
    SESSION_START,
    StaticEligibility,
    default_criteria,
    default_names,
    etf_names,
    limits,
    make_rows,
    sha,
    study_inputs,
    tag_rows,
    weekday_sessions,
)

SESSIONS = weekday_sessions(SESSION_START, 400)
HOLDOUT_DAYS = SESSIONS[-60:]


def _registered(tmp_path, **kw):
    inputs = study_inputs(tmp_path, **kw)
    study.register(inputs, hypothesis="h")
    return inputs, inputs.registry.head_hash()


def _unspent(inputs, head):
    assert inputs.registry.holdout_events() == () and inputs.registry.head_hash() == head
    assert not study.spent_ledger_path(inputs.registry).exists()


# ---- fix 1: policy values ------------------------------------------------------------------------------
BAD_POLICY = [
    ("one_shot", False),
    ("missing_evidence", "fail"),
    ("missing_evidence", "pass"),
    ("dividend_sensitivity_flip", "ignore"),
    ("annualisation_sessions", 0),
    ("annualisation_sessions", -250),
    ("annualisation_sessions", True),
    ("exclude_first_build", False),
    ("gate_scenario", "base"),
    ("benchmark", "tri"),
    ("gate_k_ticks", 0),
    ("max_drawdown_floor", "0.15"),
    ("max_drawdown_floor", "-1"),
    ("max_annualised_swaps", "-1"),
]


@pytest.mark.parametrize("key, value", BAD_POLICY)
def test_unsupported_policy_values_are_refused_at_parse_and_register(tmp_path, key, value):
    bad = default_criteria()
    bad[key] = value
    with pytest.raises(StrategyIndiaError):
        parse_criteria(bad)
    with pytest.raises(StrategyIndiaError):
        check_supported(bad)
    inputs = study_inputs(tmp_path, criteria=bad)
    with pytest.raises(StrategyIndiaError):
        study.register(inputs, hypothesis="h")
    assert not inputs.registry.path.exists()


@pytest.mark.parametrize("key, value", [item for item in BAD_POLICY if item[0] != "gate_k_ticks"])  # k is also caught at register
def test_unsupported_policy_values_are_refused_at_preflight_without_spending(tmp_path, monkeypatch, key, value):
    bad = default_criteria()
    bad[key] = value
    inputs = study_inputs(tmp_path, criteria=bad)
    monkeypatch.setattr(study, "parse_criteria", lambda criteria: dict(criteria))  # force the bad values into the seal
    study.register(inputs, hypothesis="h")
    monkeypatch.undo()
    head = inputs.registry.head_hash()
    with pytest.raises(StrategyIndiaError) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NOT spent" in str(err.value)
    _unspent(inputs, head)


def test_zero_annualisation_sessions_can_no_longer_pass_a_spent_holdout(tmp_path):
    zero = default_criteria()
    zero["annualisation_sessions"] = 0
    with pytest.raises(StrategyIndiaError, match="positive"):
        parse_criteria(zero)


# ---- fix 2: the ETF benchmark ------------------------------------------------------------------------------
def _etf_case(tmp_path, *, drop=(), updates=None, capital=None):
    sessions = weekday_sessions(SESSION_START, 400)
    rows = make_rows(sessions, default_names(10) + etf_names(), drop=drop)
    if updates:
        rows = [r.model_copy(update=updates(r)) if updates(r) else r for r in rows]
    kw = {"rows": tag_rows(rows, DividendEvents())}
    inputs = study_inputs(tmp_path, **kw)
    if capital is not None:
        inputs.limits = limits(capital_cap=capital, per_position_cap=capital)
    study.register(inputs, hypothesis="h")
    return inputs, inputs.registry.head_hash()


ETF = ETF_ISINS[0]


@pytest.mark.parametrize(
    "case, build, message",
    [
        ("bars_stop_30_sessions_early", lambda: {"drop": [(ETF, d) for d in HOLDOUT_DAYS[-30:]]}, "no bar on 30 of 60"),
        ("no_holdout_bars", lambda: {"drop": [(ETF, d) for d in HOLDOUT_DAYS]}, "no bar on 60 of 60"),
        ("one_bar_only", lambda: {"drop": [(ETF, d) for d in HOLDOUT_DAYS[1:]]}, "no bar on 59 of 60"),
        ("one_missing_in_the_middle", lambda: {"drop": [(ETF, HOLDOUT_DAYS[20])]}, "no bar on 1 of 60"),
        ("non_positive_first_close", lambda: {"updates": lambda r: {"raw_close": Decimal(0)} if (r.anchor_isin == ETF and r.trade_date == HOLDOUT_DAYS[0]) else None}, "non-positive"),
        ("non_positive_last_close", lambda: {"updates": lambda r: {"raw_close": Decimal(0)} if (r.anchor_isin == ETF and r.trade_date == HOLDOUT_DAYS[-1]) else None}, "non-positive"),
        ("capital_buys_no_share", lambda: {"capital": "100"}, "cannot buy one"),
    ],
)
def test_a_benchmark_etf_that_would_fail_or_be_truncated_refuses_without_spending(tmp_path, case, build, message):
    inputs, head = _etf_case(tmp_path, **build())
    with pytest.raises(StrategyIndiaError, match=message) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NOT spent" in str(err.value)
    _unspent(inputs, head)


def test_the_full_etf_still_runs_and_the_benchmark_covers_every_holdout_session(tmp_path):
    inputs, head = _etf_case(tmp_path)
    outcome = study.run_holdout(inputs, expected_head=head)
    unit = outcome.report.units[0]
    assert unit.etf_unknown_reason is None and unit.etf_net_return is not None


def test_a_cost_model_input_error_is_a_typed_cli_refusal(tmp_path, monkeypatch, capsys):
    def boom(config):
        raise InputError("price must be greater than zero")

    monkeypatch.setattr(study, "cli_run", boom)
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"private_dir": "x", "dataset_dir": "x", "store_root": "x", "coverage_report": "x",
                                  "registry": "x", "report_root": "x", "git_commit": "b" * 40, "fold_rules": {},
                                  "parameter_budget_n": 1, "criteria": {}}))
    assert cli.main(["run", "--config", str(config)]) == 2
    assert "cost_model_error" in capsys.readouterr().out


# ---- fix 3: the private spent ledger and the head file -------------------------------------------------------
def test_a_spent_holdout_writes_the_ledger_and_the_head_file_0600(tmp_path):
    inputs, head = _registered(tmp_path)
    reg = inputs.registry
    assert study.head_file_path(reg).read_text().strip() == head
    outcome = study.run_holdout(inputs, expected_head=head)
    ledger = study.spent_ledger_path(reg)
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["holdout_open_event_hash"] == outcome.event_hash
    assert lines[0]["holdout_range"] == study.prepare(inputs).holdout.as_payload()
    assert study.head_file_path(reg).read_text().strip() == reg.head_hash() != head
    for path in (ledger, study.head_file_path(reg)):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_deleting_the_open_event_with_the_stale_pin_cannot_reopen_the_holdout(tmp_path):
    inputs, pre_open_head = _registered(tmp_path)
    study.run_holdout(inputs, expected_head=pre_open_head)
    lines = inputs.registry.path.read_text().splitlines()
    inputs.registry.path.write_text(lines[0] + "\n")  # the open event is deleted
    assert inputs.registry.verify() == pre_open_head  # and the old pin is a perfectly valid head again
    with pytest.raises(HoldoutSpent, match="ledger"):
        study.run_holdout(inputs, expected_head=pre_open_head)
    assert inputs.registry.holdout_events() == ()  # nothing was appended, nothing was read


def test_a_malformed_ledger_refuses(tmp_path):
    inputs, head = _registered(tmp_path)
    study.spent_ledger_path(inputs.registry).write_text("not json\n")
    with pytest.raises(StrategyIndiaError, match="malformed"):
        study.run_holdout(inputs, expected_head=head)
    assert inputs.registry.holdout_events() == ()


def test_a_stale_pin_is_refused_by_the_cli_path(tmp_path, monkeypatch, capsys):
    inputs, head = _registered(tmp_path)
    study.check_pin_fresh(inputs.registry, head)
    with pytest.raises(StrategyIndiaError) as err:
        study.check_pin_fresh(inputs.registry, sha("older head"))
    assert err.value.code == "registry_head_stale"
    monkeypatch.setattr(study, "build_inputs", lambda config, **_kw: (inputs, config.get("registry_head_sha256")))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"private_dir": "x", "dataset_dir": "x", "store_root": "x", "coverage_report": "x",
                                  "registry": "x", "report_root": str(tmp_path / "out"), "git_commit": "b" * 40,
                                  "fold_rules": {}, "parameter_budget_n": 1, "criteria": {}}))
    assert cli.main(["holdout", "--config", str(config), "--registry-head", sha("older head")]) == 2
    assert "registry_head_stale" in capsys.readouterr().out
    assert inputs.registry.holdout_events() == ()


# ---- fix 7: pre-computable inputs and a typed INVALID --------------------------------------------------------
def test_missing_eligibility_inputs_for_a_holdout_session_refuse_without_spending(tmp_path):
    inputs, head = _registered(tmp_path)

    class Missing(StaticEligibility):
        def inputs_available(self, day):
            return "no ASM surveillance snapshot" if day == HOLDOUT_DAYS[10] else None

    inputs.eligibility = Missing(inputs.eligibility.eligible)
    with pytest.raises(StrategyIndiaError, match="eligibility inputs are missing") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable"
    _unspent(inputs, head)


def test_universe_eligibility_reports_a_missing_surveillance_snapshot(monkeypatch):
    seen = []

    def snapshot_for(store, kind, day):
        seen.append((kind, day))
        return None if kind == "gsm" else object()

    monkeypatch.setattr(data._surveillance, "snapshot_for", snapshot_for)
    source = data.UniverseEligibility(object(), object(), object())
    assert "GSM" in source.inputs_available(date(2026, 1, 5))
    relaxed = data.UniverseEligibility(object(), object(), object(), allow_missing_surveillance_before=date(2027, 1, 1))
    assert relaxed.inputs_available(date(2026, 1, 5)) is None  # the research waiver applies before that date


def test_a_regime_fit_that_cannot_run_on_development_data_refuses_without_spending(tmp_path, monkeypatch):
    inputs, head = _registered(tmp_path)

    def broken(*_a, **_k):
        raise StrategyIndiaError("no regime features exist up to the fitting cutoff")

    monkeypatch.setattr(study, "fit_fold_components", broken)
    with pytest.raises(StrategyIndiaError, match="regime filter") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable"
    _unspent(inputs, head)


@pytest.mark.parametrize("failure", [StrategyIndiaError("boom", code="boom_code"), ValueError("unexpected")])
def test_a_failure_after_the_open_is_recorded_as_a_typed_invalid_never_silent(tmp_path, monkeypatch, failure):
    inputs, head = _registered(tmp_path)

    def fail(*_a, **_k):
        raise failure

    monkeypatch.setattr(study, "run_holdout_segment", fail)
    with pytest.raises(HoldoutInvalid) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_invalid"
    reg = inputs.registry
    (opened,) = reg.holdout_events()
    (invalid,) = reg.invalid_events()
    assert invalid.payload["holdout_open_event_hash"] == opened.entry_hash
    assert invalid.payload["error_type"] == type(failure).__name__
    assert invalid.payload["error_code"] == getattr(failure, "code", "unexpected_error")
    assert study.head_file_path(reg).read_text().strip() == reg.head_hash()
    assert len(study.spent_ledger_path(reg).read_text().splitlines()) == 1
    monkeypatch.undo()
    with pytest.raises(HoldoutSpent):
        study.run_holdout(inputs, expected_head=reg.head_hash())  # the spend stands
