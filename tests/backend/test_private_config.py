"""Tests for the fail-closed private configuration loader (ISO-03).

All limit and strategy values in this file are test-only synthetic values.
None of them is a real limit.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from private_config import (
    SUPPORTED_WORKSPACES,
    WORKSPACE_CURRENCY,
    PrivateConfigError,
    load_workspace_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Test-only synthetic values (same as the conftest fixture).
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _params_hash(params: dict) -> str:
    encoded = json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _write_limits(private: Path, **overrides) -> None:
    payload = dict(SYNTHETIC_LIMITS)
    payload.update(overrides)
    _write(private / "india" / "limits.json", payload)


def _edit_strategy(private: Path, **overrides) -> None:
    path = private / "india" / "strategy.json"
    payload = _read(path)
    payload.update(overrides)
    _write(path, payload)


def _load_error(private, workspace="india") -> PrivateConfigError:
    with pytest.raises(PrivateConfigError) as info:
        load_workspace_config(private, workspace)
    return info.value


# --- happy path ----------------------------------------------------------------


def test_supported_workspaces_and_currency_table():
    assert SUPPORTED_WORKSPACES == frozenset({"uk", "india"})
    assert WORKSPACE_CURRENCY == {"uk": "GBP", "india": "INR"}


def test_valid_india_directory_loads(private_config_dir: Path):
    config = load_workspace_config(private_config_dir, "india")
    assert config.workspace == "india"
    assert config.currency == "INR"
    assert config.manifest is None
    assert isinstance(config.limits.capital_cap, Decimal)
    assert config.limits.capital_cap == Decimal("1000.00")
    assert config.strategy.strategy_params_version == "test-fixture-1"
    assert len(config.strategy.research_refs) == 1
    assert len(config.strategy.holdout_refs) == 1
    assert len(config.fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in config.fingerprint)


def test_accepts_str_path_and_pathlike(private_config_dir: Path):
    assert load_workspace_config(str(private_config_dir), "india").workspace == "india"


def test_repr_carries_no_limit_value(private_config_dir: Path):
    config = load_workspace_config(private_config_dir, "india")
    text = repr(config)
    assert "1000.00" not in text
    assert "250.00" not in text
    assert "india" in text
    assert config.fingerprint in text
    assert "1000.00" not in repr(config.limits)
    assert "fixture_window" not in repr(config.strategy)


def test_valid_uk_manifest_loads(private_config_dir: Path):
    config = load_workspace_config(private_config_dir, "uk")
    assert config.workspace == "uk"
    assert config.currency == "GBP"
    assert config.limits is None
    assert config.strategy is None
    assert config.manifest is not None


def test_uk_does_not_need_india_files(private_config_dir: Path):
    shutil.rmtree(private_config_dir / "india")
    assert load_workspace_config(private_config_dir, "uk").workspace == "uk"


def test_fingerprint_changes_with_file_bytes(private_config_dir: Path):
    before = load_workspace_config(private_config_dir, "india").fingerprint
    _write_limits(private_config_dir, capital_cap="1000.01", per_position_cap="250.00")
    after = load_workspace_config(private_config_dir, "india").fingerprint
    assert before != after


def test_loaded_models_are_frozen(private_config_dir: Path):
    config = load_workspace_config(private_config_dir, "india")
    with pytest.raises(Exception):
        config.limits.capital_cap = Decimal("1")
    with pytest.raises(Exception):
        config.workspace = "uk"


# --- missing directories and files ---------------------------------------------


@pytest.mark.parametrize("bad_dir", [None, "does-not-exist"])
def test_missing_private_dir_fails_closed(tmp_path: Path, bad_dir):
    target = None if bad_dir is None else tmp_path / bad_dir
    assert _load_error(target).code == "PRIVATE_DIR_MISSING"


def test_unsupported_workspace_fails_before_reading_files(tmp_path: Path):
    assert _load_error(tmp_path / "does-not-exist", "us").code == "UNSUPPORTED_WORKSPACE"


def test_missing_workspace_directory(private_config_dir: Path):
    shutil.rmtree(private_config_dir / "india")
    assert _load_error(private_config_dir).code == "WORKSPACE_DIR_MISSING"


def test_missing_strategy_file(private_config_dir: Path):
    (private_config_dir / "india" / "strategy.json").unlink()
    error = _load_error(private_config_dir)
    assert error.code == "FILE_MISSING"
    assert error.field == "strategy.json"


def test_missing_uk_manifest(private_config_dir: Path):
    (private_config_dir / "uk" / "manifest.json").unlink()
    error = _load_error(private_config_dir, "uk")
    assert error.code == "FILE_MISSING"
    assert error.field == "manifest.json"


# --- reading and parsing --------------------------------------------------------


def test_empty_and_blank_files(private_config_dir: Path):
    target = private_config_dir / "india" / "limits.json"
    target.write_bytes(b"")
    assert _load_error(private_config_dir).code == "FILE_EMPTY"
    target.write_bytes(b"  \n\t ")
    assert _load_error(private_config_dir).code == "FILE_EMPTY"


def test_non_utf8_file(private_config_dir: Path):
    (private_config_dir / "india" / "limits.json").write_bytes(b'{"a": "\xff\xfe"}')
    assert _load_error(private_config_dir).code == "NOT_UTF8"


def test_invalid_json_file(private_config_dir: Path):
    (private_config_dir / "india" / "limits.json").write_text("{not json", encoding="utf-8")
    assert _load_error(private_config_dir).code == "INVALID_JSON"


def _assert_detached(error: PrivateConfigError, marker: str) -> None:
    # No cause or context: nothing that walks the chain reaches file content.
    assert error.__cause__ is None
    assert error.__context__ is None
    assert marker not in str(error)
    assert marker not in repr(error)


def test_non_utf8_error_does_not_chain_file_bytes(private_config_dir: Path):
    (private_config_dir / "india" / "limits.json").write_bytes(b'{"k": "MARK3141", "a": "\xff"}')
    error = _load_error(private_config_dir)
    assert error.code == "NOT_UTF8"
    _assert_detached(error, "MARK3141")


def test_invalid_json_error_does_not_chain_file_text(private_config_dir: Path):
    (private_config_dir / "india" / "limits.json").write_text(
        '{"capital_cap": "MARK3141", ', encoding="utf-8"
    )
    error = _load_error(private_config_dir)
    assert error.code == "INVALID_JSON"
    _assert_detached(error, "MARK3141")


def test_duplicate_key_error_does_not_chain(private_config_dir: Path):
    (private_config_dir / "india" / "limits.json").write_text(
        '{"capital_cap": "MARK3141", "capital_cap": "1"}', encoding="utf-8"
    )
    error = _load_error(private_config_dir)
    assert error.code == "DUPLICATE_KEY"
    _assert_detached(error, "MARK3141")


def test_lone_surrogate_in_params_is_refused(private_config_dir: Path):
    _edit_strategy(private_config_dir, params={"k": "\ud800", "other": "MARK3141"})
    error = _load_error(private_config_dir)
    assert (error.code, error.field) == ("INVALID_UNICODE", "strategy.json")
    _assert_detached(error, "MARK3141")


def test_lone_surrogate_in_ref_path_is_refused(private_config_dir: Path):
    _edit_strategy(private_config_dir, research_refs=[{"path": "a\udc00b", "sha256": "0" * 64}])
    error = _load_error(private_config_dir)
    assert (error.code, error.field) == ("INVALID_UNICODE", "strategy.json")


@pytest.mark.parametrize("text", ["[]", '"x"', "5", "null"])
def test_non_object_top_level(private_config_dir: Path, text: str):
    (private_config_dir / "india" / "limits.json").write_text(text, encoding="utf-8")
    assert _load_error(private_config_dir).code == "NOT_AN_OBJECT"


def test_oversize_file_is_refused_without_parsing(private_config_dir: Path):
    target = private_config_dir / "india" / "limits.json"
    # Not valid JSON on purpose: a parse attempt would report INVALID_JSON.
    target.write_bytes(b"x" * 65537)
    assert _load_error(private_config_dir).code == "FILE_TOO_LARGE"
    # Exactly the cap is read and then fails on content, not size.
    target.write_bytes(b"x" * 65536)
    assert _load_error(private_config_dir).code == "INVALID_JSON"


def test_duplicate_key_top_level(private_config_dir: Path):
    text = (
        '{"schema_version": 1, "workspace": "india", "currency": "INR", '
        '"capital_cap": "1000.00", "capital_cap": "9999999.00", '
        '"per_position_cap": "250.00", "drawdown_halt": "-0.30", '
        '"drawdown_flatten": "-0.45", "position_stop": "-0.25"}'
    )
    (private_config_dir / "india" / "limits.json").write_text(text, encoding="utf-8")
    error = _load_error(private_config_dir)
    assert error.code == "DUPLICATE_KEY"
    assert error.field == "capital_cap"
    assert "9999999" not in str(error)


def test_duplicate_key_nested_in_params(private_config_dir: Path):
    strategy = _read(private_config_dir / "india" / "strategy.json")
    strategy.pop("params")
    body = json.dumps(strategy)[:-1]
    text = body + ', "params": {"a": 1, "nested": {"b": 1, "b": 2}}}'
    (private_config_dir / "india" / "strategy.json").write_text(text, encoding="utf-8")
    assert _load_error(private_config_dir).code == "DUPLICATE_KEY"


@pytest.mark.parametrize(
    "raw_value",
    ["1000.5", "1e3", "-0.0", "1E2"],
)
def test_json_float_in_limits_is_refused(private_config_dir: Path, raw_value: str):
    text = json.dumps(SYNTHETIC_LIMITS).replace('"1000.00"', raw_value)
    (private_config_dir / "india" / "limits.json").write_text(text, encoding="utf-8")
    error = _load_error(private_config_dir)
    assert error.code == "FLOAT_NOT_ALLOWED"
    assert raw_value not in str(error)


def test_json_float_nested_in_params_is_refused(private_config_dir: Path):
    strategy = _read(private_config_dir / "india" / "strategy.json")
    strategy.pop("params")
    text = json.dumps(strategy)[:-1] + ', "params": {"a": [1, {"b": 0.5}]}}'
    (private_config_dir / "india" / "strategy.json").write_text(text, encoding="utf-8")
    assert _load_error(private_config_dir).code == "FLOAT_NOT_ALLOWED"


def test_json_float_in_refs_is_refused(private_config_dir: Path):
    strategy = _read(private_config_dir / "india" / "strategy.json")
    strategy["research_refs"] = []
    text = json.dumps(strategy)[:-1] + ', "holdout_extra": 1.5}'
    (private_config_dir / "india" / "strategy.json").write_text(text, encoding="utf-8")
    assert _load_error(private_config_dir).code == "FLOAT_NOT_ALLOWED"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_literals_are_refused(private_config_dir: Path, literal: str):
    text = json.dumps(SYNTHETIC_LIMITS).replace('"1000.00"', literal)
    (private_config_dir / "india" / "limits.json").write_text(text, encoding="utf-8")
    assert _load_error(private_config_dir).code == "NON_FINITE_NOT_ALLOWED"


@pytest.mark.parametrize(
    "bad",
    [" 5 ", "1e3", "+5", "NaN", "Infinity", "", "5.", ".5", "1_000", "05", "0x10", "1000\n"],
)
def test_malformed_decimal_strings_are_schema_invalid(private_config_dir: Path, bad: str):
    _write_limits(private_config_dir, capital_cap=bad)
    error = _load_error(private_config_dir)
    assert error.code == "SCHEMA_INVALID"
    assert error.field == "capital_cap"


@pytest.mark.parametrize("bad", [True, None, [], {}, 1000])
def test_non_string_decimal_values_are_schema_invalid(private_config_dir: Path, bad):
    _write_limits(private_config_dir, capital_cap=bad)
    assert _load_error(private_config_dir).code == "SCHEMA_INVALID"


def test_unknown_key_is_schema_invalid_without_leaking_value(private_config_dir: Path):
    _write_limits(private_config_dir, surprise_limit="987654.32")
    error = _load_error(private_config_dir)
    assert error.code == "SCHEMA_INVALID"
    assert error.field == "surprise_limit"
    assert "SCHEMA_INVALID" in str(error)
    assert "987654.32" not in str(error)
    assert "987654.32" not in repr(error)


def test_missing_key_is_schema_invalid_not_defaulted(private_config_dir: Path):
    payload = dict(SYNTHETIC_LIMITS)
    payload.pop("position_stop")
    _write(private_config_dir / "india" / "limits.json", payload)
    error = _load_error(private_config_dir)
    assert error.code == "SCHEMA_INVALID"
    assert error.field == "position_stop"


def test_json_number_decimal_is_refused_but_string_is_accepted(private_config_dir: Path):
    _write_limits(private_config_dir, capital_cap=1000)
    error = _load_error(private_config_dir)
    assert error.code == "SCHEMA_INVALID"
    assert error.field == "capital_cap"
    _write_limits(private_config_dir, capital_cap="1000.00")
    assert load_workspace_config(private_config_dir, "india").limits.capital_cap == Decimal(
        "1000.00"
    )


def test_bad_params_version_and_empty_params(private_config_dir: Path):
    _edit_strategy(private_config_dir, strategy_params_version="has space")
    assert _load_error(private_config_dir).code == "SCHEMA_INVALID"
    _edit_strategy(private_config_dir, strategy_params_version="ok-1", params={})
    assert _load_error(private_config_dir).field == "params"


def test_bad_sha256_shape_is_schema_invalid(private_config_dir: Path):
    _edit_strategy(private_config_dir, params_sha256="ABC")
    assert _load_error(private_config_dir).code == "SCHEMA_INVALID"


# --- identity ---------------------------------------------------------------------


def test_india_limits_copied_into_uk_is_workspace_mismatch(private_config_dir: Path):
    shutil.copyfile(
        private_config_dir / "india" / "limits.json", private_config_dir / "uk" / "manifest.json"
    )
    os.chmod(private_config_dir / "uk" / "manifest.json", 0o600)
    error = _load_error(private_config_dir, "uk")
    assert error.code == "WORKSPACE_MISMATCH"


def test_uk_manifest_copied_into_india_is_workspace_mismatch(private_config_dir: Path):
    manifest = _read(private_config_dir / "uk" / "manifest.json")
    _write(private_config_dir / "india" / "limits.json", manifest)
    assert _load_error(private_config_dir).code == "WORKSPACE_MISMATCH"


def test_strategy_naming_other_workspace_is_workspace_mismatch(private_config_dir: Path):
    _edit_strategy(private_config_dir, workspace="uk")
    assert _load_error(private_config_dir).code == "WORKSPACE_MISMATCH"


def test_wrong_currency_is_refused(private_config_dir: Path):
    _write_limits(private_config_dir, currency="GBP")
    assert _load_error(private_config_dir).code == "CURRENCY_MISMATCH"
    manifest = _read(private_config_dir / "uk" / "manifest.json")
    manifest["currency"] = "INR"
    _write(private_config_dir / "uk" / "manifest.json", manifest)
    assert _load_error(private_config_dir, "uk").code == "CURRENCY_MISMATCH"


def test_wrong_schema_version_is_schema_invalid(private_config_dir: Path):
    _write_limits(private_config_dir, schema_version=2)
    assert _load_error(private_config_dir).code == "SCHEMA_INVALID"


@pytest.mark.parametrize("bad", [True, False, "1", None])
def test_non_integer_schema_version_is_schema_invalid(private_config_dir: Path, bad):
    # JSON true would otherwise coerce to 1 and pass Literal[1].
    _write_limits(private_config_dir, schema_version=bad)
    error = _load_error(private_config_dir)
    assert (error.code, error.field) == ("SCHEMA_INVALID", "schema_version")


def test_bool_schema_version_refused_in_strategy_and_manifest(private_config_dir: Path):
    _edit_strategy(private_config_dir, schema_version=True)
    error = _load_error(private_config_dir)
    assert (error.code, error.field) == ("SCHEMA_INVALID", "schema_version")
    manifest_path = private_config_dir / "uk" / "manifest.json"
    manifest = _read(manifest_path)
    manifest["schema_version"] = True
    _write(manifest_path, manifest)
    error = _load_error(private_config_dir, "uk")
    assert (error.code, error.field) == ("SCHEMA_INVALID", "schema_version")


def test_schema_error_does_not_chain_validation_error(private_config_dir: Path):
    _write_limits(private_config_dir, capital_cap="MARK3141")
    error = _load_error(private_config_dir)
    assert (error.code, error.field) == ("SCHEMA_INVALID", "capital_cap")
    _assert_detached(error, "MARK3141")


# --- limit ordering --------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"capital_cap": "0"}, "capital_cap"),
        ({"capital_cap": "-5"}, "capital_cap"),
        ({"per_position_cap": "0"}, "per_position_cap"),
        ({"per_position_cap": "1000.01"}, "per_position_cap"),
        ({"drawdown_halt": "0"}, "drawdown_halt"),
        ({"drawdown_halt": "0.10"}, "drawdown_halt"),
        ({"drawdown_flatten": "-0.30"}, "drawdown_flatten"),
        ({"drawdown_flatten": "-0.10"}, "drawdown_flatten"),
        ({"drawdown_flatten": "-1"}, "drawdown_flatten"),
        ({"position_stop": "0"}, "position_stop"),
        ({"position_stop": "-1"}, "position_stop"),
        ({"position_stop": "0.10"}, "position_stop"),
    ],
)
def test_limit_ordering_violations(private_config_dir: Path, overrides: dict, field: str):
    _write_limits(private_config_dir, **overrides)
    error = _load_error(private_config_dir)
    assert error.code == "LIMIT_ORDER_INVALID"
    assert error.field == field


def test_per_position_cap_may_equal_capital_cap(private_config_dir: Path):
    _write_limits(private_config_dir, per_position_cap="1000.00")
    assert load_workspace_config(private_config_dir, "india").limits.per_position_cap == Decimal(
        "1000.00"
    )


# --- params hash --------------------------------------------------------------------


def test_params_value_change_without_hash_update(private_config_dir: Path):
    strategy = _read(private_config_dir / "india" / "strategy.json")
    strategy["params"]["fixture_window"] = 4
    _write(private_config_dir / "india" / "strategy.json", strategy)
    assert _load_error(private_config_dir).code == "PARAMS_HASH_MISMATCH"


def test_params_hash_is_key_order_independent(private_config_dir: Path):
    params = {"fixture_window": 3, "fixture_label": "synthetic"}
    _edit_strategy(private_config_dir, params=params, params_sha256=_params_hash(params))
    assert load_workspace_config(private_config_dir, "india").strategy.params == params


# --- filesystem: symlinks, types, permissions, owner ----------------------------


def test_symlinked_private_dir_is_refused(private_config_dir: Path, tmp_path: Path):
    link = tmp_path / "private-link"
    link.symlink_to(private_config_dir, target_is_directory=True)
    assert _load_error(link).code == "SYMLINK_REFUSED"


def test_symlinked_workspace_dir_is_refused(private_config_dir: Path):
    real = private_config_dir / "india-real"
    (private_config_dir / "india").rename(real)
    (private_config_dir / "india").symlink_to(real, target_is_directory=True)
    assert _load_error(private_config_dir).code == "SYMLINK_REFUSED"


def test_symlinked_config_file_is_refused(private_config_dir: Path, tmp_path: Path):
    target = private_config_dir / "india" / "limits.json"
    real = tmp_path / "elsewhere.json"
    shutil.copyfile(target, real)
    os.chmod(real, 0o600)
    target.unlink()
    target.symlink_to(real)
    assert _load_error(private_config_dir).code == "SYMLINK_REFUSED"


def test_symlinked_ref_file_is_refused(private_config_dir: Path, tmp_path: Path):
    ref = private_config_dir / "india" / "research" / "fixture-research.json"
    real = tmp_path / "ref-elsewhere.json"
    shutil.copyfile(ref, real)
    os.chmod(real, 0o600)
    ref.unlink()
    ref.symlink_to(real)
    assert _load_error(private_config_dir).code == "SYMLINK_REFUSED"


def test_directory_where_file_expected(private_config_dir: Path):
    target = private_config_dir / "india" / "limits.json"
    target.unlink()
    target.mkdir()
    os.chmod(target, 0o700)
    assert _load_error(private_config_dir).code == "NOT_A_REGULAR_FILE"


def test_file_where_directory_expected(private_config_dir: Path):
    shutil.rmtree(private_config_dir / "india")
    (private_config_dir / "india").write_text("x", encoding="utf-8")
    os.chmod(private_config_dir / "india", 0o600)
    assert _load_error(private_config_dir).code == "NOT_A_DIRECTORY"


def test_private_dir_that_is_a_file(tmp_path: Path):
    target = tmp_path / "private"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o600)
    assert _load_error(target).code == "NOT_A_DIRECTORY"


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755, 0o770])
@pytest.mark.parametrize("which", ["private", "workspace"])
def test_open_directory_permissions_are_refused(private_config_dir: Path, which: str, mode: int):
    target = private_config_dir if which == "private" else private_config_dir / "india"
    os.chmod(target, mode)
    try:
        error = _load_error(private_config_dir)
    finally:
        os.chmod(target, 0o700)
    assert error.code == "PERMISSIONS_TOO_OPEN"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
@pytest.mark.parametrize("which", ["limits.json", "strategy.json"])
def test_open_config_file_permissions_are_refused(private_config_dir: Path, which: str, mode: int):
    target = private_config_dir / "india" / which
    os.chmod(target, mode)
    error = _load_error(private_config_dir)
    assert error.code == "PERMISSIONS_TOO_OPEN"
    assert error.field == which


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_open_ref_file_permissions_are_refused(private_config_dir: Path, mode: int):
    os.chmod(private_config_dir / "india" / "holdout" / "fixture-holdout.json", mode)
    error = _load_error(private_config_dir)
    assert error.code == "PERMISSIONS_TOO_OPEN"
    assert error.field == "holdout_refs.0"


def test_owner_mismatch_is_refused(private_config_dir: Path, monkeypatch: pytest.MonkeyPatch):
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    error = _load_error(private_config_dir)
    assert error.code == "OWNER_MISMATCH"
    assert error.field == "private_dir"


def test_owner_mismatch_on_config_file(private_config_dir: Path, monkeypatch: pytest.MonkeyPatch):
    import private_config.loader as loader_module

    real_uid = os.getuid()
    calls = {"count": 0}

    def uid_flip():
        # private dir and workspace dir are checked first; flip for the files.
        calls["count"] += 1
        return real_uid if calls["count"] <= 2 else real_uid + 1

    monkeypatch.setattr(loader_module.os, "getuid", uid_flip)
    error = _load_error(private_config_dir)
    assert error.code == "OWNER_MISMATCH"
    assert error.field.endswith(".json")


# --- research and holdout references ----------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        ".",
        "/etc/hostname",
        "../uk/manifest.json",
        "research/../../uk/manifest.json",
        "research/../research/fixture-research.json",
        "research\\fixture-research.json",
        "research/\x00x",
    ],
)
def test_invalid_ref_paths(private_config_dir: Path, bad_path: str):
    research_sha = _sha(
        (private_config_dir / "india" / "research" / "fixture-research.json").read_bytes()
    )
    _edit_strategy(private_config_dir, research_refs=[{"path": bad_path, "sha256": research_sha}])
    error = _load_error(private_config_dir)
    assert error.code == "REF_PATH_INVALID"
    assert error.field == "research_refs.0"


def test_ref_through_symlinked_directory_is_refused(private_config_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside-research"
    shutil.copytree(private_config_dir / "india" / "research", outside)
    shutil.rmtree(private_config_dir / "india" / "research")
    (private_config_dir / "india" / "research").symlink_to(outside, target_is_directory=True)
    assert _load_error(private_config_dir).code == "REF_PATH_INVALID"


def test_ref_to_directory_is_refused(private_config_dir: Path):
    _edit_strategy(
        private_config_dir, research_refs=[{"path": "research", "sha256": _sha(b"anything")}]
    )
    assert _load_error(private_config_dir).code == "NOT_A_REGULAR_FILE"


def test_missing_ref_file(private_config_dir: Path):
    (private_config_dir / "india" / "holdout" / "fixture-holdout.json").unlink()
    error = _load_error(private_config_dir)
    assert error.code == "REF_MISSING"
    assert error.field == "holdout_refs.0"


def test_missing_ref_intermediate_directory(private_config_dir: Path):
    shutil.rmtree(private_config_dir / "india" / "research")
    assert _load_error(private_config_dir).code == "REF_MISSING"


def test_changed_ref_file_is_hash_mismatch(private_config_dir: Path):
    target = private_config_dir / "india" / "research" / "fixture-research.json"
    target.write_text(json.dumps({"fixture": "tampered"}), encoding="utf-8")
    error = _load_error(private_config_dir)
    assert error.code == "REF_HASH_MISMATCH"
    assert error.field == "research_refs.0"


def test_valid_ref_in_nested_directory_loads(private_config_dir: Path):
    nested = private_config_dir / "india" / "research" / "deep"
    nested.mkdir()
    os.chmod(nested, 0o700)
    data = b'{"fixture": "nested"}'
    target = nested / "result.json"
    target.write_bytes(data)
    os.chmod(target, 0o600)
    _edit_strategy(
        private_config_dir,
        research_refs=[{"path": "research/deep/./result.json", "sha256": _sha(data)}],
    )
    config = load_workspace_config(private_config_dir, "india")
    assert config.strategy.research_refs[0].sha256 == _sha(data)


def test_large_ref_file_is_streamed_and_verified(private_config_dir: Path):
    data = b"a" * (3 * 1024 * 1024 + 17)
    target = private_config_dir / "india" / "research" / "big.bin"
    target.write_bytes(data)
    os.chmod(target, 0o600)
    _edit_strategy(
        private_config_dir, research_refs=[{"path": "research/big.bin", "sha256": _sha(data)}]
    )
    assert load_workspace_config(private_config_dir, "india").workspace == "india"
    _edit_strategy(
        private_config_dir, research_refs=[{"path": "research/big.bin", "sha256": _sha(b"x")}]
    )
    assert _load_error(private_config_dir).code == "REF_HASH_MISMATCH"


# --- decoupling and leak guards -----------------------------------------------------


def test_package_reads_no_environment_and_imports_no_execution():
    package = REPO_ROOT / "backend" / "private_config"
    sources = sorted(package.glob("*.py"))
    assert sources, "backend/private_config/*.py not found"
    offences: list[str] = []
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name
                    if top in ("execution", "backend.execution") or top.startswith(
                        ("execution.", "backend.execution.")
                    ):
                        offences.append(f"{source.name}:{node.lineno} import {top}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in ("execution", "backend.execution") or module.startswith(
                    ("execution.", "backend.execution.")
                ):
                    offences.append(f"{source.name}:{node.lineno} from {module}")
                if module == "backend" and any(a.name == "execution" for a in node.names):
                    offences.append(f"{source.name}:{node.lineno} from backend import execution")
                if module == "os" and any(a.name in ("environ", "getenv") for a in node.names):
                    offences.append(f"{source.name}:{node.lineno} from os import env access")
            elif isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"):
                offences.append(f"{source.name}:{node.lineno} .{node.attr}")
            elif isinstance(node, ast.Name) and node.id in ("getenv", "environ"):
                offences.append(f"{source.name}:{node.lineno} {node.id}")
    assert offences == []


def test_private_dir_is_gitignored_and_untracked():
    if shutil.which("git") is None:
        pytest.fail("git is required for the private/ leak guard")
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "private/india/limits.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.returncode == 0, "private/ is not gitignored"
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "private"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr
    assert tracked.stdout.strip() == "", "files under private/ are tracked"
