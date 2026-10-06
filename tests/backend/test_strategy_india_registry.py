"""AC-3: registration seal (D-10, D-11). Append-only hash chain, live-input check, import hygiene."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from strategy_india import study
from strategy_india.errors import HoldoutSpent, RegistryError, RegistryMismatch
from strategy_india.holdout import default_criteria
from strategy_india.registry import (
    HASH_FIELDS,
    LIVE_CHECKED_FIELDS,
    REGISTRATION_FIELDS,
    Registry,
    check_live_inputs,
    criteria_hash,
)

from test_strategy_india_support import registration_record, sha

REGISTRY_SRC = Path(__file__).resolve().parents[2] / "backend" / "strategy_india" / "registry.py"


def _registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "private" / "registry.jsonl")


def test_record_holds_every_d10_and_d19_field():
    wanted = {
        "hypothesis", "parameter_budget_n", "params_sha256", "dataset_sha256", "coverage_report_sha256", "git_commit",
        "seed", "benchmark_ids", "fill_scenarios_sha256", "charge_schedule_sha256", "tick_table_sha256",
        "fold_rules", "holdout_range", "holdout_sha256", "holdout_criteria", "holdout_criteria_sha256",
        "hurdle_map_sha256", "charge_schedule_version",
    }
    assert wanted <= set(REGISTRATION_FIELDS)


def test_chain_verifies_and_head_moves(tmp_path):
    reg = _registry(tmp_path)
    first = reg.register(registration_record())
    second = reg.register(registration_record(seed=8))
    assert reg.verify() == second.entry_hash
    assert second.prev_hash == first.entry_hash
    assert [e.seq for e in reg.entries()] == [0, 1]
    assert reg.registration().entry_hash == second.entry_hash  # the latest registration is the live one


def _lines(reg: Registry) -> list[str]:
    return reg.path.read_text().splitlines()


def test_editing_any_entry_breaks_verification(tmp_path):
    reg = _registry(tmp_path)
    reg.register(registration_record())
    reg.register(registration_record(seed=8))
    lines = _lines(reg)
    for index in (0, 1):
        edited = [json.loads(line) for line in lines]
        edited[index]["payload"]["seed"] = 999
        reg.path.write_text("\n".join(json.dumps(item) for item in edited) + "\n")
        with pytest.raises(RegistryError):
            reg.verify()
    reg.path.write_text("\n".join(lines) + "\n")
    reg.verify()


def test_deleting_any_entry_breaks_verification(tmp_path):
    reg = _registry(tmp_path)
    for seed in (1, 2, 3):
        reg.register(registration_record(seed=seed))
    lines = _lines(reg)
    head = reg.verify()
    for drop in (0, 1):
        reg.path.write_text("\n".join(line for i, line in enumerate(lines) if i != drop) + "\n")
        with pytest.raises(RegistryError):
            reg.verify()
    reg.path.write_text("\n".join(lines[:-1]) + "\n")  # tail removal keeps a valid chain...
    reg.verify()
    with pytest.raises(RegistryError):  # ...so the pinned head hash is what catches it
        reg.verify(expected_head=head)
    reg.path.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
    with pytest.raises(RegistryError):
        reg.verify()


def test_registration_rejects_bad_shape(tmp_path):
    reg = _registry(tmp_path)
    record = registration_record()
    for broken in (
        {k: v for k, v in record.items() if k != "params_sha256"},
        {**record, "extra": 1},
        {**record, "params_sha256": "XYZ"},
        {**record, "git_commit": "abc"},
        {**record, "holdout_criteria": {}},
        {**record, "holdout_criteria_sha256": sha("different")},
        {**record, "parameter_budget_n": 0},
        {**record, "seed": 1.5},
    ):
        with pytest.raises(RegistryError):
            reg.register(broken)
    assert not reg.path.exists()


@pytest.mark.parametrize("field", LIVE_CHECKED_FIELDS)
def test_any_differing_live_field_refuses(field):
    record = registration_record()
    live = {name: record[name] for name in LIVE_CHECKED_FIELDS}
    check_live_inputs(record, live)  # identical inputs pass
    live[field] = _different(record[field])
    with pytest.raises(RegistryMismatch) as err:
        check_live_inputs(record, live)
    assert err.value.field == field


def _different(value):
    if isinstance(value, str):
        return sha("different" + value) if len(value) == 64 else value + "x"
    if isinstance(value, int):
        return value + 1
    if isinstance(value, list):
        return [*value, "extra"]
    if isinstance(value, dict):
        return {**value, "extra": "1"}
    raise AssertionError(type(value))


def test_missing_live_input_refuses():
    record = registration_record()
    live = {name: record[name] for name in LIVE_CHECKED_FIELDS}
    del live["dataset_sha256"]
    with pytest.raises(RegistryMismatch):
        check_live_inputs(record, live)


def test_every_hash_field_is_live_checked():
    assert set(HASH_FIELDS) <= set(LIVE_CHECKED_FIELDS)


def test_run_refused_when_record_missing(tmp_path):
    record = registration_record()
    live = {name: record[name] for name in LIVE_CHECKED_FIELDS}
    with pytest.raises(RegistryError):
        study.require_registration(Registry(tmp_path / "none.jsonl"), live, expected_head=None)
    empty = Registry(tmp_path / "empty.jsonl")
    empty.path.write_text("")
    with pytest.raises(RegistryError, match="no registration"):
        study.require_registration(empty, live, expected_head=None)


@pytest.mark.parametrize("field", LIVE_CHECKED_FIELDS)
def test_run_refused_when_any_live_input_differs(tmp_path, field):
    reg = _registry(tmp_path)
    record = registration_record()
    reg.register(record)
    live = {name: record[name] for name in LIVE_CHECKED_FIELDS}
    assert study.require_registration(reg, live, expected_head=None).payload["seed"] == 7
    live[field] = _different(record[field])
    with pytest.raises(RegistryMismatch):
        study.require_registration(reg, live, expected_head=None)


def test_new_registration_must_cite_spent_events_and_not_overlap(tmp_path):
    from strategy_india.holdout import open_holdout

    reg = _registry(tmp_path)
    reg.register(registration_record())
    criteria = default_criteria()
    open_holdout(reg, criteria=criteria, expected_head=reg.verify())
    spent = [e.entry_hash for e in reg.holdout_events()]
    with pytest.raises(HoldoutSpent):  # overlaps the spent range
        reg.register(registration_record(spent_holdout_event_hashes=spent))
    later = {"start": "2026-07-01", "end": "2026-12-31"}
    with pytest.raises(RegistryError, match="cite"):
        reg.register(registration_record(holdout_range=later))
    reg.register(registration_record(holdout_range=later, spent_holdout_event_hashes=spent))


def test_registry_imports_neither_pilot_data_nor_costs():
    tree = ast.parse(REGISTRY_SRC.read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert not roots & {"pilot_data", "costs", "private_config", "execution", "gateway"}, roots


def test_registry_refuses_floats():
    with pytest.raises(RegistryError):
        criteria_hash({"x": 0.5})
