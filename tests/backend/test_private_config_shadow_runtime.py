"""The fail-closed loader for ``private/india/shadow_runtime.json`` (64-09 Task B). Synthetic values only."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from private_config import PrivateConfigError, ShadowRuntime, load_shadow_runtime_config

CANARY_STORE = "/CANARY-SESSION-STORE-ROOT"
CANARY_KEY = "ab" * 32
SHA = "cd" * 32


def good() -> dict:
    return {
        "schema_version": 1, "workspace": "india", "session_store_root": CANARY_STORE,
        "dataset_dir": "/CANARY-DATASET", "calendar_path": "/CANARY-CALENDAR.json", "calendar_sha256": SHA,
        "simulation_root": "/CANARY-SIM", "window_start": "2025-03-03", "vm_key_id": CANARY_KEY,
    }


def write(private: Path, body: object, *, mode: int = 0o600) -> Path:
    workspace = private / "india"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    private.chmod(0o700)
    path = workspace / "shadow_runtime.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    path.chmod(mode)
    return path


def code_of(private: Path | None) -> str:
    with pytest.raises(PrivateConfigError) as caught:
        load_shadow_runtime_config(private)
    for text in (str(caught.value), repr(caught.value), caught.value.field):
        assert CANARY_STORE not in text and CANARY_KEY not in text and "CANARY" not in text
    return caught.value.code


def test_a_valid_file_loads_and_its_repr_names_no_value(tmp_path):
    write(tmp_path, good())
    config = load_shadow_runtime_config(tmp_path)
    assert isinstance(config, ShadowRuntime)
    assert config.session_store_root == CANARY_STORE and config.window_start == date(2025, 3, 3)
    assert config.vm_key_id == CANARY_KEY
    assert "CANARY" not in repr(config) and CANARY_KEY not in repr(config) and SHA not in repr(config)


def test_a_missing_dir_workspace_or_file_is_an_error_never_a_default(tmp_path):
    assert code_of(None) == "PRIVATE_DIR_MISSING"
    assert code_of(tmp_path / "nope") == "PRIVATE_DIR_MISSING"
    assert code_of(tmp_path) == "WORKSPACE_DIR_MISSING"
    (tmp_path / "india").mkdir(mode=0o700)
    assert code_of(tmp_path) == "SHADOW_RUNTIME_MISSING"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({**good(), "session_store_root": "relative/path"}, "SCHEMA_INVALID"),
        ({**good(), "dataset_dir": ""}, "SCHEMA_INVALID"),
        ({**good(), "calendar_path": "/a\x00b"}, "SCHEMA_INVALID"),
        ({**good(), "calendar_sha256": "ABCD"}, "SCHEMA_INVALID"),
        ({**good(), "calendar_sha256": "CD" * 32}, "SCHEMA_INVALID"),
        ({**good(), "vm_key_id": "AB" * 32}, "SCHEMA_INVALID"),
        ({**good(), "vm_key_id": CANARY_KEY[:-2]}, "SCHEMA_INVALID"),
        ({**good(), "window_start": "2025-3-3"}, "SCHEMA_INVALID"),
        ({**good(), "window_start": 20250303}, "SCHEMA_INVALID"),
        ({**good(), "window_start": "2025-02-30"}, "SCHEMA_INVALID"),
        ({**good(), "extra": "x"}, "SCHEMA_INVALID"),
        ({**good(), "schema_version": 2}, "SCHEMA_INVALID"),
        ({**good(), "workspace": "uk"}, "WORKSPACE_MISMATCH"),
        ({k: v for k, v in good().items() if k != "vm_key_id"}, "SCHEMA_INVALID"),
        ("[]", "NOT_AN_OBJECT"),
        ("", "FILE_EMPTY"),
        ('{"schema_version": 1, "schema_version": 1}', "DUPLICATE_KEY"),
    ],
)
def test_a_bad_file_is_refused_with_a_code_and_no_value(tmp_path, body, expected):
    write(tmp_path, body)
    assert code_of(tmp_path) == expected


def test_a_symlinked_or_loose_file_is_refused(tmp_path):
    real = write(tmp_path / "real", good())
    private = tmp_path / "private"
    (private / "india").mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    os.symlink(real, private / "india" / "shadow_runtime.json")
    assert code_of(private) == "SYMLINK_REFUSED"
    loose = tmp_path / "loose"
    write(loose, good(), mode=0o644)
    assert code_of(loose) == "PERMISSIONS_TOO_OPEN"
