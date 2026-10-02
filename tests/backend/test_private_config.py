"""Tests for the fail-closed private configuration loader (ISO-03)."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from private_config import (
    SUPPORTED_WORKSPACES,
    WORKSPACE_CURRENCY,
    PrivateConfigError,
    load_workspace_config,
)

# Test-only synthetic values. They are deliberately not real limits.
SYNTHETIC_LIMITS = {
    "schema_version": 1,
    "workspace": "india",
    "currency": "INR",
    "capital_cap": "1000.00",
    "per_position_cap": "250.00",
    "drawdown_halt": "-0.30",
    "drawdown_flatten": "-0.45",
    "position_stop": "-0.25",
}
SYNTHETIC_PARAMS = {"fixture_label": "synthetic", "fixture_window": 3}


def _params_hash(params: dict) -> str:
    encoded = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _build_dir(root: Path) -> Path:
    private = root / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    for workspace in ("uk", "india"):
        directory = private / workspace
        directory.mkdir()
        os.chmod(directory, 0o700)
    _write_json(
        private / "uk" / "manifest.json",
        {"schema_version": 1, "workspace": "uk", "currency": "GBP"},
    )
    _write_json(private / "india" / "limits.json", dict(SYNTHETIC_LIMITS))
    _write_json(
        private / "india" / "strategy.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "strategy_params_version": "test-fixture-1",
            "params": dict(SYNTHETIC_PARAMS),
            "params_sha256": _params_hash(SYNTHETIC_PARAMS),
            "research_refs": [],
            "holdout_refs": [],
        },
    )
    return private


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    return _build_dir(tmp_path)


def test_supported_workspaces_and_currency_table():
    assert SUPPORTED_WORKSPACES == frozenset({"uk", "india"})
    assert WORKSPACE_CURRENCY == {"uk": "GBP", "india": "INR"}


def test_valid_india_directory_loads(private_dir: Path):
    config = load_workspace_config(private_dir, "india")
    assert config.workspace == "india"
    assert config.currency == "INR"
    assert config.manifest is None
    assert isinstance(config.limits.capital_cap, Decimal)
    assert config.limits.capital_cap == Decimal("1000.00")
    assert config.strategy.strategy_params_version == "test-fixture-1"
    assert len(config.fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in config.fingerprint)


def test_repr_carries_no_limit_value(private_dir: Path):
    config = load_workspace_config(private_dir, "india")
    text = repr(config)
    assert "1000.00" not in text
    assert "250.00" not in text
    assert "india" in text
    assert config.fingerprint in text


def test_valid_uk_manifest_loads(private_dir: Path):
    config = load_workspace_config(private_dir, "uk")
    assert config.workspace == "uk"
    assert config.currency == "GBP"
    assert config.limits is None
    assert config.strategy is None
    assert config.manifest is not None


@pytest.mark.parametrize("bad_dir", [None, "does-not-exist"])
def test_missing_private_dir_fails_closed(tmp_path: Path, bad_dir):
    target = None if bad_dir is None else tmp_path / bad_dir
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(target, "india")
    assert info.value.code == "PRIVATE_DIR_MISSING"


def test_unsupported_workspace_fails_before_reading_files(tmp_path: Path):
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(tmp_path / "does-not-exist", "us")
    assert info.value.code == "UNSUPPORTED_WORKSPACE"


def test_missing_workspace_directory(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(private, "india")
    assert info.value.code == "WORKSPACE_DIR_MISSING"


def test_missing_strategy_file(private_dir: Path):
    (private_dir / "india" / "strategy.json").unlink()
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(private_dir, "india")
    assert info.value.code == "FILE_MISSING"
    assert info.value.field == "strategy.json"


def test_unknown_key_is_schema_invalid_without_leaking_value(private_dir: Path):
    payload = dict(SYNTHETIC_LIMITS)
    payload["surprise_limit"] = "987654.32"
    _write_json(private_dir / "india" / "limits.json", payload)
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(private_dir, "india")
    assert info.value.code == "SCHEMA_INVALID"
    assert info.value.field == "surprise_limit"
    assert "SCHEMA_INVALID" in str(info.value)
    assert "987654.32" not in str(info.value)
    assert "987654.32" not in repr(info.value)


def test_json_number_decimal_is_refused_but_string_is_accepted(private_dir: Path):
    payload = dict(SYNTHETIC_LIMITS)
    payload["capital_cap"] = 1000
    _write_json(private_dir / "india" / "limits.json", payload)
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(private_dir, "india")
    assert info.value.code == "SCHEMA_INVALID"
    assert info.value.field == "capital_cap"

    payload["capital_cap"] = "1000.00"
    _write_json(private_dir / "india" / "limits.json", payload)
    assert load_workspace_config(private_dir, "india").limits.capital_cap == Decimal("1000.00")
