"""Review round 1: the seal and its trust anchors.

Holdout pre-flight (no spend on a known failure), the sealed D-20 event list and sensitivity factor,
the required registry head pin (58 holdout_refs or an explicit flag) and the private D-19 criteria file.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from private_config.loader import load_workspace_config

from strategy_india import __main__ as cli
from strategy_india import holdout, study
from strategy_india.data import DividendEvents, DividendUnknownEvent
from strategy_india.errors import DataError, RegistryError, RegistryMismatch, StrategyIndiaError
from strategy_india.holdout import load_criteria_file, parse_criteria
from strategy_india.params import params_sha256, placeholder_params
from strategy_india.signals import MODE_SENSITIVITY, SignalTable
from strategy_india.ticks import EQUITY, NON_GOLD_ETF, TickTables, load_default_tables, load_table

from test_strategy_india_support import (
    FIXTURE_DIR,
    SESSION_START,
    default_criteria,
    default_names,
    make_rows,
    params,
    sha,
    study_inputs,
    weekday_sessions,
)

SESSIONS = weekday_sessions(SESSION_START, 400)
ANCHOR = "INE000A01000"


def _registered(tmp_path, **kw):
    inputs = study_inputs(tmp_path, **kw)
    entry = study.register(inputs, hypothesis="h")
    return inputs, entry, inputs.registry.head_hash()


def _equity_only() -> TickTables:
    return TickTables({EQUITY: load_default_tables().table_for(EQUITY)})


# ---- fix 1: pre-flight, then spend ----------------------------------------------------------------
def test_a_missing_etf_tick_table_refuses_without_spending_the_holdout(tmp_path):
    inputs, _, head = _registered(tmp_path, ticks=_equity_only())
    with pytest.raises(StrategyIndiaError) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NON_GOLD_ETF" in str(err.value) and "NOT spent" in str(err.value)
    assert inputs.registry.holdout_events() == () and inputs.registry.head_hash() == head


def test_an_etf_table_that_stops_before_the_holdout_refuses_without_spending_it(tmp_path):
    raw = json.loads((FIXTURE_DIR / "synthetic_etf_tick_table.json").read_text())
    raw["versions"][0]["effective_to"] = "2025-12-31"
    short = tmp_path / "etf_short.json"
    short.write_text(json.dumps(raw))
    ticks = TickTables({EQUITY: load_default_tables().table_for(EQUITY), NON_GOLD_ETF: load_table(short)})
    inputs, _, head = _registered(tmp_path, ticks=ticks)
    holdout_days = [d for d in SESSIONS[-60:] if d > date(2025, 12, 31)]
    assert holdout_days, "the synthetic holdout runs past the table"
    with pytest.raises(StrategyIndiaError, match="does not cover") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NON_GOLD_ETF" in str(err.value)
    assert inputs.registry.holdout_events() == ()


def test_equity_ticks_missing_for_holdout_dates_refuse_without_spending(tmp_path):
    inputs, _, head = _registered(tmp_path, start=date(2023, 6, 1))  # the whole window precedes 2025-04-15
    with pytest.raises(StrategyIndiaError, match="EQUITY") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and inputs.registry.holdout_events() == ()


def test_a_dataset_that_does_not_reproduce_its_hash_refuses_without_spending(tmp_path):
    inputs = study_inputs(tmp_path)
    inputs.dataset_sha256 = sha("not the real dataset hash")  # sealed as given, but the rows do not match it
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    with pytest.raises(StrategyIndiaError, match="dataset_sha256") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and inputs.registry.holdout_events() == ()


def test_a_runnable_holdout_still_opens_exactly_once(tmp_path):
    inputs, _, head = _registered(tmp_path)
    outcome = study.run_holdout(inputs, expected_head=head)
    assert len(inputs.registry.holdout_events()) == 1 and outcome.report.units[0].etf_unknown_reason is None
    assert outcome.report.units[0].etf_net_return is not None


# ---- fix 2: the D-20 event list is sealed ------------------------------------------------------------
EV = DividendUnknownEvent(ANCHOR, "EV1", SESSIONS[250])


def test_the_event_list_hash_is_canonical_sorted_and_defined_for_empty():
    a = DividendEvents([DividendUnknownEvent("B", "E2", date(2025, 7, 1)), DividendUnknownEvent("A", "E1", date(2025, 6, 2))])
    b = DividendEvents([DividendUnknownEvent("A", "E1", date(2025, 6, 2)), DividendUnknownEvent("B", "E2", date(2025, 7, 1))])
    assert a.sealed_sha256() == b.sealed_sha256() != DividendEvents().sealed_sha256()
    assert DividendEvents().sealed_sha256() == DividendEvents([]).sealed_sha256()
    assert [e.event_id for e in a.all()] == ["E1", "E2"]
    with pytest.raises(DataError):
        DividendEvents([DividendUnknownEvent("A", "", date(2025, 6, 2))])
    with pytest.raises(DataError):
        DividendEvents([DividendUnknownEvent("A", "E1", date(2025, 6, 2)), DividendUnknownEvent("A", "E1", date(2025, 6, 3))])


@pytest.mark.parametrize(
    "changed",
    [
        DividendEvents(),  # emptied after a non-empty list was sealed
        DividendEvents([DividendUnknownEvent(ANCHOR, "EV1", SESSIONS[251])]),  # a moved ex-date
        DividendEvents([DividendUnknownEvent(ANCHOR, "EV9", SESSIONS[250])]),  # a different event id
        DividendEvents([EV, DividendUnknownEvent("INE000A01001", "EV2", SESSIONS[260])]),  # an extra event
    ],
    ids=["empty", "moved", "renamed", "extra"],
)
def test_a_changed_or_empty_event_list_is_refused_for_research_and_the_holdout(tmp_path, changed):
    inputs = study_inputs(tmp_path, events=DividendEvents([EV]))
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    study.run_research(inputs, expected_head=head)  # the sealed list runs
    inputs.events = changed
    with pytest.raises(RegistryMismatch) as err:
        study.run_research(inputs, expected_head=head)
    assert err.value.field == "dividend_events_sha256"
    with pytest.raises(RegistryMismatch):
        study.run_holdout(inputs, expected_head=head)
    assert inputs.registry.holdout_events() == ()


def test_an_empty_list_is_allowed_only_when_it_was_sealed_empty(tmp_path):
    inputs = study_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    study.run_research(inputs, expected_head=head)
    inputs.events = DividendEvents([EV])
    with pytest.raises(RegistryMismatch) as err:
        study.run_research(inputs, expected_head=head)
    assert err.value.field == "dividend_events_sha256"


# ---- fix 3: the sealed sensitivity factor -------------------------------------------------------------
def test_the_sealed_factor_is_used_for_the_sensitivity_run_and_a_different_one_is_refused(tmp_path):
    sealed = default_criteria()
    sealed["dividend_sensitivity_factor"] = "0.95"
    events = DividendEvents([EV])
    inputs = study_inputs(tmp_path / "a", events=events, criteria=sealed, ex_gaps={(ANCHOR, SESSIONS[250]): Decimal("-0.06")})
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    report = study.run_research(inputs, expected_head=head)
    assert report.dividend.sensitivity_factor == Decimal("0.95")
    other = study_inputs(tmp_path / "b", events=events, ex_gaps={(ANCHOR, SESSIONS[250]): Decimal("-0.06")})
    study.register(other, hypothesis="h")
    default_report = study.run_research(other, expected_head=other.registry.head_hash())
    assert default_report.dividend.sensitivity_factor == Decimal("0.98")
    nets = lambda r: [u.dividend_sensitivity.net_return for u in r.units]  # noqa: E731
    assert nets(report) != nets(default_report), "a different sealed factor gives a different sensitivity run"
    inputs.sensitivity_factor = Decimal("0.98")  # the caller disagrees with what was sealed
    with pytest.raises(RegistryMismatch) as err:
        study.run_research(inputs, expected_head=head)
    assert err.value.field == "dividend_sensitivity_factor"
    with pytest.raises(RegistryMismatch):
        study.run_holdout(inputs, expected_head=head)
    inputs.sensitivity_factor = Decimal("0.95")
    study.run_research(inputs, expected_head=head)


def test_the_sensitivity_table_needs_a_valid_factor_and_criteria_refuse_a_bad_one():
    from strategy_india.data import DatasetView
    from strategy_india.holdout import HoldoutRange

    rows = make_rows(SESSIONS[:60], default_names(3))
    view = DatasetView.from_rows(rows, holdout=HoldoutRange(date(2030, 1, 1), date(2030, 2, 1)))
    for bad in (None, Decimal(0), Decimal(1), Decimal("1.2")):
        with pytest.raises(ValueError):
            SignalTable(view, params(), DividendEvents([EV]), mode=MODE_SENSITIVITY, sensitivity_factor=bad)
    for bad in ("1.5", "0", "-0.1", "x"):
        broken = default_criteria()
        broken["dividend_sensitivity_factor"] = bad
        with pytest.raises(RegistryError):
            parse_criteria(broken)


# ---- fix 7: the head pin ----------------------------------------------------------------------------------
def test_run_research_and_holdout_need_a_pin(tmp_path):
    inputs, _, head = _registered(tmp_path)
    with pytest.raises(TypeError):
        study.run_research(inputs)  # no keyword at all
    with pytest.raises(RegistryError, match="pinned"):
        study.run_research(inputs, expected_head=None)
    with pytest.raises(RegistryError, match="pinned"):
        study.run_holdout(inputs, expected_head="")
    assert inputs.registry.holdout_events() == ()


def test_a_truncated_chain_with_an_old_pin_refuses_research_and_holdout(tmp_path):
    inputs, _, head1 = _registered(tmp_path)
    study.register(inputs, hypothesis="again", expected_head=head1)
    head2 = inputs.registry.head_hash()
    lines = inputs.registry.path.read_text().splitlines()
    inputs.registry.path.write_text(lines[0] + "\n")
    for call in (lambda: study.run_research(inputs, expected_head=head2), lambda: study.run_holdout(inputs, expected_head=head2)):
        with pytest.raises(RegistryError, match="head"):
            call()


def test_a_second_registration_needs_the_pinned_head(tmp_path):
    inputs, _, head = _registered(tmp_path)
    with pytest.raises(RegistryError, match="pinned"):
        study.register(inputs, hypothesis="again")
    with pytest.raises(RegistryError, match="head"):
        study.register(inputs, hypothesis="again", expected_head=sha("wrong"))
    study.register(inputs, hypothesis="again", expected_head=head)


def _private_dir(tmp_path: Path, *, head: str | None, ref_head: bool = True, criteria: dict | None = None) -> tuple[Path, Path]:
    private = tmp_path / "private"
    ws = private / "india"
    ws.mkdir(parents=True)
    os.chmod(private, 0o700)
    os.chmod(ws, 0o700)

    def put(name: str, text: str) -> str:
        path = ws / name
        path.write_text(text)
        os.chmod(path, 0o600)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    put("limits.json", json.dumps({"schema_version": 1, "workspace": "india", "currency": "INR", "capital_cap": "50000",
                                   "per_position_cap": "10000", "drawdown_halt": "-0.08", "drawdown_flatten": "-0.15",
                                   "position_stop": "-0.12"}))
    refs = []
    if head is not None and ref_head:
        refs.append({"path": study.HEAD_REF_NAME, "sha256": put(study.HEAD_REF_NAME, head + "\n")})
    if criteria is not None:
        put(holdout.CRITERIA_FILE_NAME, json.dumps(criteria))
    params_raw = placeholder_params()
    put("strategy.json", json.dumps({"schema_version": 1, "workspace": "india", "strategy_params_version": "synthetic-1",
                                     "params": params_raw, "params_sha256": params_sha256(params_raw),
                                     "research_refs": [], "holdout_refs": refs}))
    return private, ws


def test_the_pin_is_read_from_the_58_holdout_refs(tmp_path):
    head = sha("a registry head")
    private, ws = _private_dir(tmp_path, head=head)
    cfg = load_workspace_config(private, "india")  # 58 verifies the pin file's sha256 as part of the load
    assert study.resolve_registry_head({}, cfg.strategy.holdout_refs, ws) == head
    assert study.resolve_registry_head({"registry_head_sha256": head}, cfg.strategy.holdout_refs, ws) == head
    with pytest.raises(StrategyIndiaError, match="differs"):
        study.resolve_registry_head({"registry_head_sha256": sha("other")}, cfg.strategy.holdout_refs, ws)
    with pytest.raises(StrategyIndiaError, match="sha256"):
        study.resolve_registry_head({"registry_head_sha256": "nothex"}, cfg.strategy.holdout_refs, ws)


def test_without_a_ref_the_pin_must_be_explicit(tmp_path):
    private, ws = _private_dir(tmp_path, head=None)
    cfg = load_workspace_config(private, "india")
    with pytest.raises(StrategyIndiaError) as err:
        study.resolve_registry_head({}, cfg.strategy.holdout_refs, ws)
    assert err.value.code == "registry_head_missing"
    head = sha("explicit")
    assert study.resolve_registry_head({"registry_head_sha256": head}, cfg.strategy.holdout_refs, ws) == head


def test_cli_registers_and_prints_the_new_head_and_run_and_holdout_need_a_pin(tmp_path, monkeypatch, capsys):
    inputs = study_inputs(tmp_path)
    monkeypatch.setattr(study, "build_inputs", lambda config: (inputs, config.get("registry_head_sha256")))
    config = tmp_path / "config.json"
    base = {"private_dir": "x", "dataset_dir": "x", "store_root": "x", "coverage_report": "x", "registry": "x",
            "report_root": str(tmp_path / "out"), "git_commit": "b" * 40, "fold_rules": {"n_folds": 1, "test_sessions": 1, "min_train_sessions": 1},
            "parameter_budget_n": 12, "criteria": {"path": "x", "sha256": "x"}}
    config.write_text(json.dumps(base))
    assert cli.main(["register", "--config", str(config)]) == 0
    printed = json.loads(capsys.readouterr().out)
    head = printed["registry_head"]
    assert head == inputs.registry.head_hash() and "pin" in printed["next"]
    assert cli.main(["run", "--config", str(config)]) == 2  # no pin anywhere
    assert "registry_head_missing" in capsys.readouterr().out
    assert cli.main(["holdout", "--config", str(config)]) == 2
    capsys.readouterr()
    assert inputs.registry.holdout_events() == ()
    assert cli.main(["holdout", "--config", str(config), "--registry-head", head]) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["registry_head"] == inputs.registry.head_hash() != head and after["verdict"] in ("PASS", "FAIL", "INCONCLUSIVE")
    assert cli.main(["run", "--config", str(config), "--registry-head", head]) == 2  # the old pin no longer matches the chain


# ---- fix 8: D-19 criteria are a private file ----------------------------------------------------------------
def _file_with(tmp_path, criteria) -> tuple[Path, str]:
    ws = tmp_path / "private" / "india"
    ws.mkdir(parents=True, exist_ok=True)
    path = ws / holdout.CRITERIA_FILE_NAME
    path.write_text(json.dumps(criteria))
    return ws, hashlib.sha256(path.read_bytes()).hexdigest()


def test_criteria_load_by_path_plus_sha256_and_refuse_every_deviation(tmp_path):
    ws, digest = _file_with(tmp_path, default_criteria())
    assert load_criteria_file(ws, holdout.CRITERIA_FILE_NAME, digest) == default_criteria()
    for rel, sha_ in ((holdout.CRITERIA_FILE_NAME, None), ("", digest), (holdout.CRITERIA_FILE_NAME, sha("other")),
                      ("missing.json", digest), ("../escape.json", digest), ("/abs.json", digest)):
        with pytest.raises(RegistryError):
            load_criteria_file(ws, rel, sha_)
    broken = default_criteria()
    del broken["one_shot"]
    ws2, digest2 = _file_with(tmp_path / "b", broken)
    with pytest.raises(RegistryError, match="keys"):
        load_criteria_file(ws2, holdout.CRITERIA_FILE_NAME, digest2)
    floaty = tmp_path / "c" / "private" / "india"
    floaty.mkdir(parents=True)
    text = json.dumps(default_criteria()).replace('"-0.15"', "-0.15")
    (floaty / holdout.CRITERIA_FILE_NAME).write_text(text)
    with pytest.raises(RegistryError):
        load_criteria_file(floaty, holdout.CRITERIA_FILE_NAME, hashlib.sha256(text.encode()).hexdigest())


def test_a_symlinked_criteria_file_is_refused(tmp_path):
    ws, digest = _file_with(tmp_path, default_criteria())
    link = ws / "link.json"
    link.symlink_to(ws / holdout.CRITERIA_FILE_NAME)
    with pytest.raises(RegistryError):
        load_criteria_file(ws, "link.json", digest)


def test_tracked_code_carries_no_criteria_values_and_a_study_without_criteria_refuses(tmp_path):
    assert not hasattr(holdout, "D19_TEMPLATE") and not hasattr(holdout, "default_criteria")
    assert set(holdout.CRITERIA_SCHEMA) == set(default_criteria()), "the fixture example matches the schema exactly"
    inputs = study_inputs(tmp_path)
    inputs.criteria = None
    with pytest.raises(RegistryError, match="criteria are absent"):
        study.register(inputs, hypothesis="h")
    inputs.criteria = default_criteria()
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    inputs.criteria = None
    with pytest.raises(RegistryError, match="criteria are absent"):
        study.run_research(inputs, expected_head=head)
    changed = default_criteria()
    changed["max_annualised_swaps"] = "52"
    inputs.criteria = changed  # a different file than the sealed one
    with pytest.raises(RegistryMismatch) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.field == "holdout_criteria_sha256" and inputs.registry.holdout_events() == ()
