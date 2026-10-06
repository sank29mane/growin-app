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
from strategy_india.holdout import check_supported, load_criteria_file, parse_criteria

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
    assert len(lines) == 1 and lines[0]["registration_entry_hash"] == outcome.report.registration_entry_hash
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


# Review round 3: durable spends and results, shared freshness checks, typed refusals.
def _cli_config(tmp_path, inputs, head, monkeypatch):
    monkeypatch.setattr(study, "build_inputs", lambda *_a, **_k: (inputs, head))
    config = tmp_path / "cli.json"
    config.write_text(json.dumps({"private_dir": "x", "dataset_dir": "x", "store_root": "x",
                                  "coverage_report": "x", "registry": "x", "report_root": str(tmp_path / "out"),
                                  "git_commit": "b" * 40, "fold_rules": {}, "parameter_budget_n": 1, "criteria": {}}))
    return config


@pytest.mark.parametrize("step", ["open_return", "write_head_file", "view_open", "context",
                                   "run_holdout_segment", "_etf", "holdout_evidence", "evaluate_verdict",
                                   "choose_etf", "_tri", "build_report", "append_holdout_verdict", "verdict_head"])
def test_each_post_open_failure_leaves_durable_spend_and_invalid(tmp_path, monkeypatch, step):
    inputs, head = _registered(tmp_path)
    reg = inputs.registry

    def crash(*_a, **_k):
        raise OSError("injected post-open crash")

    if step == "open_return":
        original = study.open_holdout
        def open_then_crash(*a, **k):
            original(*a, **k)
            crash()
        monkeypatch.setattr(study, "open_holdout", open_then_crash)
    elif step == "view_open":
        monkeypatch.setattr(data.DatasetView, "open", crash)
    elif step == "append_holdout_verdict":
        monkeypatch.setattr(reg, "append_holdout_verdict", crash)
    elif step in ("context", "verdict_head"):
        name = "_context" if step == "context" else "write_head_file"
        original = getattr(study, name)
        calls = 0
        def fail_second(*a, **k):
            nonlocal calls
            calls += 1
            if calls == 2:
                crash()
            return original(*a, **k)
        monkeypatch.setattr(study, name, fail_second)
    else:
        monkeypatch.setattr(study, step, crash)
    with pytest.raises(HoldoutInvalid):
        study.run_holdout(inputs, expected_head=head)
    assert len(study.spent_ledger_path(reg).read_text().splitlines()) == 1
    assert len(reg.holdout_events()) == len(reg.invalid_events()) == 1
    assert reg.invalid_events()[0].payload["holdout_open_event_hash"] == reg.holdout_events()[0].entry_hash


def test_read_only_ledger_refuses_before_open(tmp_path, monkeypatch):
    inputs, head = _registered(tmp_path)
    ledger = study.spent_ledger_path(inputs.registry)
    ledger.touch(mode=0o600)
    ledger.chmod(0o400)
    real_open = os.open
    def deny_write(path, flags, *a, **k):
        # Inject the OS refusal too, so the test remains meaningful when run as root.
        if path == ledger and flags & os.O_WRONLY:
            raise PermissionError("ledger is read-only")
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", deny_write)
    with pytest.raises(StrategyIndiaError, match="cannot be written"):
        study.run_holdout(inputs, expected_head=head)
    assert ledger.read_text() == ""
    assert inputs.registry.head_hash() == head
    assert inputs.registry.holdout_events() == ()


def test_report_failure_preserves_registry_verdict_and_prints_anchor(tmp_path, monkeypatch, capsys):
    inputs, head = _registered(tmp_path)
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    def fail_report(*a, **k):
        verdicts = [e for e in inputs.registry.entries() if e.kind == "holdout_verdict"]
        assert len(verdicts) == 1  # ordering: verdict is durable before the attempted report write
        raise OSError("report directory is read-only")
    monkeypatch.setattr(study, "write_report", fail_report)
    assert cli.main(["holdout", "--config", str(config)]) == 2
    output = capsys.readouterr().out
    assert "holdout_invalid" in output and "Phase 58 holdout_refs" in output and "phase SUMMARY" in output
    verdict = next(e for e in inputs.registry.entries() if e.kind == "holdout_verdict")
    from strategy_india.registry import canonical_sha256
    assert verdict.payload["verdict"] in {"PASS", "FAIL", "INCONCLUSIVE"}
    assert verdict.payload["verdict_sha256"] == canonical_sha256(verdict.payload["verdict_payload"])
    assert len(study.spent_ledger_path(inputs.registry).read_text().splitlines()) == 1
    assert len(inputs.registry.invalid_events()) == 1


def test_library_refuses_deleted_open_and_deleted_ledger_with_stale_head(tmp_path):
    inputs, head = _registered(tmp_path)
    study.run_holdout(inputs, expected_head=head)
    inputs.registry.path.write_text(inputs.registry.path.read_text().splitlines()[0] + "\n")
    study.spent_ledger_path(inputs.registry).unlink()
    with pytest.raises(StrategyIndiaError) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "registry_head_stale"
    assert inputs.registry.holdout_events() == ()


def test_cli_refuses_nonempty_registry_with_missing_head(tmp_path, monkeypatch, capsys):
    inputs, head = _registered(tmp_path)
    study.head_file_path(inputs.registry).unlink()
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    assert cli.main(["holdout", "--config", str(config)]) == 2
    assert "registry_head_stale" in capsys.readouterr().out
    _unspent(inputs, head)


@pytest.mark.parametrize("kind", ["unreadable", "directory"])
def test_cli_ledger_io_errors_are_typed_refusals(tmp_path, monkeypatch, capsys, kind):
    inputs, head = _registered(tmp_path)
    ledger = study.spent_ledger_path(inputs.registry)
    if kind == "directory":
        ledger.mkdir()
    else:
        ledger.touch()
        original = type(ledger).read_text
        def unreadable(path, *a, **k):
            if path == ledger:
                raise PermissionError("unreadable ledger")
            return original(path, *a, **k)
        monkeypatch.setattr(type(ledger), "read_text", unreadable)
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    assert cli.main(["holdout", "--config", str(config)]) == 2
    assert "ledger_invalid" in capsys.readouterr().out
    assert inputs.registry.holdout_events() == ()
    assert inputs.registry.head_hash() == head


@pytest.mark.parametrize("value", ["NaN", "sNaN"])
@pytest.mark.parametrize("key", ["max_drawdown_floor", "max_annualised_swaps", "dividend_sensitivity_factor"])
def test_cli_nonfinite_criteria_are_typed_refusals(tmp_path, monkeypatch, capsys, key, value):
    inputs, head = _registered(tmp_path)
    inputs.criteria[key] = value
    with pytest.raises(RegistryError, match="decimal string"):
        parse_criteria(dict(inputs.criteria))
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    import hashlib
    criteria_file = tmp_path / "criteria.json"
    criteria_file.write_text(json.dumps(inputs.criteria))
    digest = hashlib.sha256(criteria_file.read_bytes()).hexdigest()
    def build_with_real_criteria_loader(*_a, **_k):
        inputs.criteria = load_criteria_file(tmp_path, criteria_file.name, digest)
        return inputs, head
    monkeypatch.setattr(study, "build_inputs", build_with_real_criteria_loader)
    assert cli.main(["holdout", "--config", str(config)]) == 2
    assert "registry_error" in capsys.readouterr().out
    _unspent(inputs, head)


def test_partially_overlapping_spent_ledger_range_refuses_library_open(tmp_path):
    inputs, head = _registered(tmp_path)
    from strategy_india.holdout import HoldoutRange
    partial = HoldoutRange(SESSIONS[-70], HOLDOUT_DAYS[10])
    registered = HoldoutRange(HOLDOUT_DAYS[0], HOLDOUT_DAYS[-1])
    assert partial != registered and partial.overlaps(registered)
    ledger = study.spent_ledger_path(inputs.registry)
    ledger.write_text(json.dumps({"holdout_range": partial.as_payload()}) + "\n")
    with pytest.raises(HoldoutSpent, match="ledger"):
        study.run_holdout(inputs, expected_head=head)
    assert inputs.registry.holdout_events() == ()
    assert inputs.registry.head_hash() == head
    assert len(ledger.read_text().splitlines()) == 1


def test_successful_cli_open_prints_external_anchor_and_records_verdict(tmp_path, monkeypatch, capsys):
    inputs, head = _registered(tmp_path)
    config = _cli_config(tmp_path, inputs, head, monkeypatch)
    assert cli.main(["holdout", "--config", str(config)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert "Phase 58 holdout_refs" in summary["next"] and "phase SUMMARY" in summary["next"]
    assert inputs.registry.head_hash() in summary["next"]
    assert inputs.registry.entries()[-1].kind == "holdout_verdict"
    assert study.head_file_path(inputs.registry).read_text().strip() == inputs.registry.head_hash()


@pytest.mark.parametrize("step", ["partial_write", "fsync"])
def test_failed_ledger_reservation_leaves_nothing_spent(tmp_path, monkeypatch, step):
    inputs, head = _registered(tmp_path)
    ledger = study.spent_ledger_path(inputs.registry)
    ledger.touch()
    original_write, original_fsync = os.write, os.fsync
    def is_ledger(fd):
        return os.fstat(fd).st_ino == ledger.stat().st_ino
    def broken_write(fd, data):
        if is_ledger(fd):
            original_write(fd, data[:10])
            raise OSError("partial ledger write")
        return original_write(fd, data)
    def broken_fsync(fd):
        if is_ledger(fd):
            raise OSError("ledger fsync failed")
        return original_fsync(fd)
    monkeypatch.setattr(os, "write" if step == "partial_write" else "fsync",
                        broken_write if step == "partial_write" else broken_fsync)
    with pytest.raises(StrategyIndiaError, match="cannot be written"):
        study.run_holdout(inputs, expected_head=head)
    assert ledger.read_text() == ""
    assert inputs.registry.head_hash() == head
    assert inputs.registry.holdout_events() == ()


@pytest.mark.parametrize("value", ["NaN", "sNaN"])
@pytest.mark.parametrize("key", ["max_drawdown_floor", "max_annualised_swaps"])
def test_supported_criteria_refuses_nonfinite_threshold_without_decimal_trap(key, value):
    criteria = default_criteria()
    criteria[key] = value
    with pytest.raises(StrategyIndiaError, match="finite"):
        check_supported(criteria)



def test_deleted_ledger_rerun_does_not_invalidate_the_original_verdict(tmp_path):
    inputs, head = _registered(tmp_path)
    study.run_holdout(inputs, expected_head=head)
    reg = inputs.registry
    original_head = reg.head_hash()
    original_entries = reg.entries()
    ledger = study.spent_ledger_path(reg)
    ledger.unlink()  # one accidentally deleted file, with registry and head intact
    with pytest.raises(HoldoutSpent, match="registry"):
        study.run_holdout(inputs, expected_head=original_head)
    assert reg.entries() == original_entries
    assert reg.head_hash() == original_head
    assert reg.invalid_events() == ()
    assert not ledger.exists()
