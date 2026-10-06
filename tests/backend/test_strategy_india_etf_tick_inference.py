"""A5 end to end: the Phase 62 holdout pre-flight with the benchmark ETF tick inferred for 2025-04-15..2026-09-06.

Synthetic study inputs whose holdout sits inside the window the committed ETF schedule leaves uncovered.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from strategy_india import __main__ as cli
from strategy_india import study
from strategy_india.errors import RegistryMismatch, StrategyIndiaError
from strategy_india.ticks import load_default_tables

from test_strategy_india_support import ETF_ISINS, TARGETS_SHA, default_criteria, limits, study_inputs

IN_GAP = date(2024, 1, 1)  # 500 weekday sessions end in late 2025; the 60-session holdout sits inside the window


def _inferred_inputs(tmp_path, *, sessions_n: int = 500, rows=None, **kw):
    inputs = study_inputs(tmp_path, sessions_n=sessions_n, start=IN_GAP, rows=rows, **kw)
    inputs.ticks = load_default_tables(rows=inputs.rows, benchmark_isins=ETF_ISINS[:1])
    return inputs


def test_the_holdout_preflight_passes_in_the_gap_with_an_inferred_etf_tick(tmp_path):
    inputs = _inferred_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    (record,) = inputs.ticks.inference_provenance()
    assert record["status"] == "inferred" and record["tick"] == "0.01" and record["security"] == ETF_ISINS[0]
    outcome = study.run_holdout(inputs, expected_head=head)
    assert outcome.report.units[0].etf_unknown_reason is None and outcome.report.units[0].etf_net_return is not None
    assert len(inputs.registry.holdout_events()) == 1


def test_the_same_gap_without_inference_still_refuses_without_spending(tmp_path):
    inputs = study_inputs(tmp_path, sessions_n=500, start=IN_GAP)
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    with pytest.raises(StrategyIndiaError, match="NON_GOLD_ETF.*does not cover") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and inputs.registry.holdout_events() == ()


def test_a_thin_sample_refuses_without_spending_and_names_the_reason(tmp_path):
    inputs = _inferred_inputs(tmp_path, sessions_n=400)  # only ~65 sessions fall inside the window
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    with pytest.raises(StrategyIndiaError, match="NON_GOLD_ETF.*does not cover.*inferred tick unavailable.*sample too small") as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.code == "holdout_unrunnable" and "NOT spent" in str(err.value)
    assert inputs.registry.holdout_events() == () and inputs.registry.head_hash() == head


def test_only_the_registered_benchmark_may_use_an_inferred_tick(tmp_path):
    # Inference exists for the OTHER candidate only; the registered benchmark (the more liquid ETF1) has none.
    inputs = study_inputs(tmp_path, sessions_n=500, start=IN_GAP)
    inputs.ticks = load_default_tables(rows=inputs.rows, benchmark_isins=ETF_ISINS[1:])
    entry = study.register(inputs, hypothesis="h")
    assert entry.payload["benchmark_ids"][0] == ETF_ISINS[0]
    with pytest.raises(StrategyIndiaError, match="NON_GOLD_ETF.*does not cover") as err:
        study.run_holdout(inputs, expected_head=inputs.registry.head_hash())
    assert err.value.code == "holdout_unrunnable" and inputs.registry.holdout_events() == ()


def test_the_registration_seals_the_inference_provenance(tmp_path):
    plain = study_inputs(tmp_path / "a", sessions_n=500, start=IN_GAP)
    inferred = _inferred_inputs(tmp_path / "b")
    assert plain.ticks.sha256() != inferred.ticks.sha256()
    entry = study.register(inferred, hypothesis="h")
    assert entry.payload["tick_table_sha256"] == inferred.ticks.sha256()


def test_changed_benchmark_rows_break_the_seal_even_when_the_dataset_hash_is_left_alone(tmp_path):
    inputs = _inferred_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    target = next(i for i, r in enumerate(inputs.rows)
                  if r.anchor_isin == ETF_ISINS[0] and r.trade_date > date(2025, 6, 1))
    edited = list(inputs.rows)
    edited[target] = edited[target].model_copy(update={"raw_open": edited[target].raw_open + 1})
    inputs.rows = edited  # dataset_sha256 is deliberately NOT recomputed: only the tick seal can catch this
    inputs.ticks = load_default_tables(rows=edited, benchmark_isins=ETF_ISINS[:1])
    with pytest.raises(RegistryMismatch) as err:
        study.run_holdout(inputs, expected_head=head)
    assert err.value.field == "tick_table_sha256" and inputs.registry.holdout_events() == ()


def test_cli_register_prints_only_the_tick_the_method_and_the_provenance_hash(tmp_path, monkeypatch, capsys):
    inputs = _inferred_inputs(tmp_path)
    monkeypatch.setattr(study, "build_inputs", lambda config, **_kw: (inputs, config.get("registry_head_sha256")))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"private_dir": "x", "dataset_dir": "x", "store_root": "x", "coverage_report": "x",
                                  "registry": "x", "report_root": str(tmp_path / "out"), "git_commit": "b" * 40,
                                  "fold_rules": {"n_folds": 1, "test_sessions": 1, "min_train_sessions": 1},
                                  "parameter_budget_n": 12, "criteria": {"path": "x", "sha256": "x"}}))
    assert cli.main(["register", "--config", str(config)]) == 0
    printed = json.loads(capsys.readouterr().out)
    (record,) = printed["etf_tick_inference"]
    assert record["tick"] == "0.01" and record["method"] and record["provenance_sha256"]
    # D-12: the inference reads holdout sessions, so no sample count, price or input hash is printed.
    assert set(record) == {"security", "status", "tick", "method", "provenance_sha256"}


def test_build_inputs_wires_the_inferred_etf_tick_into_the_tick_tables(tmp_path, monkeypatch):
    """The production ``build_inputs`` path, not a hand-built StudyInputs. Reverting its ``load_default_tables``
    call to the plain one leaves the window uncovered and fails every assertion below."""
    from types import SimpleNamespace

    import pilot_data.price_bands as bands_mod
    import pilot_data.store as store_mod
    import pilot_data.targets as targets_mod
    import private_config.loader as loader_mod
    from strategy_india import data as data_mod
    from strategy_india import holdout as holdout_mod
    from strategy_india.data import DividendEvents
    from strategy_india.params import placeholder_params
    from strategy_india.ticks import NON_GOLD_ETF

    synthetic = study_inputs(tmp_path / "src", sessions_n=500, start=IN_GAP)
    cfg = SimpleNamespace(strategy=SimpleNamespace(params=placeholder_params(), holdout_refs=()), limits=limits())
    monkeypatch.setattr(loader_mod, "load_workspace_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(study, "load_bound_dataset", lambda *_a, **_k: (SimpleNamespace(dataset_sha256=synthetic.dataset_sha256), synthetic.rows))
    monkeypatch.setattr(store_mod, "PilotDataStore", lambda *_a, **_k: object())
    monkeypatch.setattr(targets_mod, "latest_target_universe", lambda *_a, **_k: SimpleNamespace(target_sha256=TARGETS_SHA))
    monkeypatch.setattr(bands_mod, "BandResolver", lambda *_a, **_k: object())
    monkeypatch.setattr(data_mod, "events_from_manifest", lambda _manifest: DividendEvents())
    monkeypatch.setattr(holdout_mod, "load_criteria_file", lambda *_a, **_k: default_criteria())
    config = {
        "private_dir": str(tmp_path), "dataset_dir": "unused", "store_root": str(tmp_path), "coverage_report": str(tmp_path / "cov.json"),
        "registry": str(tmp_path / "registry.jsonl"), "git_commit": "b" * 40, "parameter_budget_n": 12,
        "fold_rules": {"n_folds": 3, "test_sessions": 50, "min_train_sessions": 120}, "criteria": {"path": "x", "sha256": "x"},
    }
    inputs, _pin = study.build_inputs(config, bind_to_registration=False)

    records = inputs.ticks.inference_provenance()  # both configured benchmark candidates
    assert [r["security"] for r in records] == sorted(ETF_ISINS)
    assert {(r["status"], r["tick"]) for r in records} == {("inferred", "0.01")}
    assert inputs.ticks.covers(NON_GOLD_ETF, date(2025, 8, 1), series="EQ", security=ETF_ISINS[0])
    assert inputs.ticks.sha256() == load_default_tables(rows=synthetic.rows, benchmark_isins=ETF_ISINS).sha256()
    assert inputs.ticks.sha256() != load_default_tables().sha256()
