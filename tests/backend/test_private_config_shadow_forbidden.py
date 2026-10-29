"""The fail-closed loader for ``private/india/shadow_forbidden.json`` (64-03 follow-up, T-64-15). Synthetic values only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from private_config import PrivateConfigError, load_shadow_forbidden_literals

CANARY_ACCOUNT = "CANARY-ACCT-31337"
CANARY_HOST = "CANARY-HOST-LITERAL"


def write(private: Path, body: object, *, mode: int = 0o600) -> Path:
    workspace = private / "india"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    private.chmod(0o700)
    path = workspace / "shadow_forbidden.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    path.chmod(mode)
    return path


def good() -> dict:
    return {"schema_version": 1, "workspace": "india", "literals": [CANARY_ACCOUNT, CANARY_HOST]}


def code_of(private: Path | None) -> str:
    with pytest.raises(PrivateConfigError) as caught:
        load_shadow_forbidden_literals(private)
    for text in (str(caught.value), repr(caught.value), caught.value.field):
        assert CANARY_ACCOUNT not in text and CANARY_HOST not in text
    return caught.value.code


def test_a_valid_file_returns_the_literals(tmp_path):
    write(tmp_path, good())
    assert load_shadow_forbidden_literals(tmp_path) == (CANARY_ACCOUNT, CANARY_HOST)


def test_a_missing_dir_workspace_or_file_is_an_error_never_an_empty_list(tmp_path):
    assert code_of(None) == "PRIVATE_DIR_MISSING"
    assert code_of(tmp_path / "nope") == "PRIVATE_DIR_MISSING"
    assert code_of(tmp_path) == "WORKSPACE_DIR_MISSING"
    (tmp_path / "india").mkdir(mode=0o700)
    assert code_of(tmp_path) == "FORBIDDEN_LITERALS_MISSING"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({**good(), "literals": []}, "SCHEMA_INVALID"),
        ({**good(), "literals": ["short"]}, "SCHEMA_INVALID"),
        ({**good(), "literals": [f" {CANARY_ACCOUNT}"]}, "SCHEMA_INVALID"),
        ({**good(), "literals": [CANARY_ACCOUNT, 7]}, "SCHEMA_INVALID"),
        ({**good(), "literals": CANARY_ACCOUNT}, "SCHEMA_INVALID"),
        ({**good(), "extra": "x"}, "SCHEMA_INVALID"),
        ({**good(), "schema_version": 2}, "SCHEMA_INVALID"),
        ({**good(), "workspace": "uk"}, "WORKSPACE_MISMATCH"),
        ({"schema_version": 1, "workspace": "india"}, "SCHEMA_INVALID"),
        ("[]", "NOT_AN_OBJECT"),
        ("", "FILE_EMPTY"),
        ('{"schema_version": 1, "schema_version": 1}', "DUPLICATE_KEY"),
        ('{"schema_version": 1, "workspace": "india", "literals": [1.5]}', "FLOAT_NOT_ALLOWED"),
    ],
)
def test_a_bad_file_is_refused_with_a_code_and_no_literal(tmp_path, body, expected):
    write(tmp_path, body)
    assert code_of(tmp_path) == expected


def test_a_symlinked_or_loose_file_is_refused(tmp_path):
    real = write(tmp_path / "real", good())
    private = tmp_path / "private"
    (private / "india").mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    os.symlink(real, private / "india" / "shadow_forbidden.json")
    assert code_of(private) == "SYMLINK_REFUSED"
    loose = tmp_path / "loose"
    write(loose, good(), mode=0o644)
    assert code_of(loose) == "PERMISSIONS_TOO_OPEN"
