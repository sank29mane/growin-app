"""Purity and strict-input guard for backend/costs.

The import allow-list is closed on purpose. Anything outside it (numpy,
pandas, random, secrets, uuid, time, os, subprocess, socket, requests, httpx,
a broker SDK, simulation, execution, utils) fails the scan. If a check fails
because the package is too lenient, tighten the package; never widen the list.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import (
    InputError,
    ScheduleError,
    canonical_json,
    canonical_value,
    strict_decimal,
    strict_int,
)
from costs.schedule import load_schedule_set

COSTS_DIR = Path(__file__).resolve().parents[2] / "backend" / "costs"
SCHEDULE_PATH = COSTS_DIR / "schedules" / "icici_nse_cash_charges.json"

ALLOWED_IMPORTS = {
    "__future__", "collections", "dataclasses", "datetime", "decimal", "enum",
    "functools", "hashlib", "itertools", "json", "operator", "pathlib", "re",
    "types", "typing",
}
BANNED_CALL_NAMES = {"float", "hash", "id", "eval", "exec", "compile", "__import__", "getcontext", "setcontext"}
BANNED_ATTR_CALLS = {"now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp", "getcontext", "setcontext"}


def scan_source(source: str, label: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source, filename=label)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORTS:
                    violations.append(f"{label}:{line}: import {alias.name} is not allow-listed")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                violations.append(f"{label}:{line}: from {node.module} import ... is not allow-listed")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                violations.append(f"{label}:{line}: call to {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in BANNED_ATTR_CALLS:
                violations.append(f"{label}:{line}: call to .{func.attr}()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (float, complex)):
            violations.append(f"{label}:{line}: float or complex literal {node.value!r}")
    return violations


def test_package_is_pure():
    files = sorted(COSTS_DIR.rglob("*.py"))
    assert len(files) >= 6, f"expected at least 6 modules under {COSTS_DIR}, found {len(files)}"
    violations: list[str] = []
    for path in files:
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(COSTS_DIR.parent))))
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("import random", "random"),
        ("import os.path", "os.path"),
        ("from time import time", "time"),
        ("from simulation import engine", "simulation"),
        ("from execution.ledger import canonical_json", "execution"),
        ("import numpy as np", "numpy"),
        ("x = float('1')", "float()"),
        ("x = 0.5", "float or complex literal"),
        ("x = hash('a')", "hash()"),
        ("import decimal\nx = decimal.getcontext()", ".getcontext()"),
        ("import datetime\nx = datetime.datetime.now()", ".now()"),
        ("import datetime\nx = datetime.date.today()", ".today()"),
        ("eval('1')", "eval()"),
    ],
)
def test_scanner_flags_violations(source, fragment):
    violations = scan_source(source, "snippet.py")
    assert violations and any(fragment in v for v in violations), violations


def test_scanner_accepts_allowed_code():
    source = "from __future__ import annotations\nimport hashlib\nfrom decimal import Decimal\nfrom .core import x\n"
    assert scan_source(source, "ok.py") == []


@pytest.mark.parametrize("good, expected", [("0.0007", Decimal("0.0007")), (Decimal("4.90"), Decimal("4.90")), (70, Decimal(70))])
def test_strict_decimal_accepts(good, expected):
    assert strict_decimal(good, "x") == expected


@pytest.mark.parametrize(
    "bad",
    [0.1, True, False, None, "NaN", "sNaN", "Infinity", "-inf", "", "  ", "1,000", "₹10",
     Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), "1e5", " 1", "0x10", [], b"1"],
)
def test_strict_decimal_rejects(bad):
    with pytest.raises(InputError):
        strict_decimal(bad, "x")


def test_strict_decimal_error_names_the_field():
    with pytest.raises(InputError, match="delivery_rate"):
        strict_decimal(0.1, "delivery_rate")


def test_strict_int():
    assert strict_int(70, "q", minimum=1) == 70
    for bad in (True, 1.0, Decimal("70"), "70", None):
        with pytest.raises(InputError):
            strict_int(bad, "q", minimum=0)
    with pytest.raises(InputError):
        strict_int(0, "q", minimum=1)
    assert strict_int(0, "q", minimum=0) == 0


def test_canonical_value_rejects_float_and_naive_datetime():
    with pytest.raises(TypeError):
        canonical_value(0.5)
    with pytest.raises(TypeError):
        canonical_value({"a": [1, 0.5]})
    with pytest.raises(TypeError):
        canonical_value(datetime(2026, 10, 5, 9, 0))
    with pytest.raises(TypeError):
        canonical_value(Decimal("NaN"))


def test_canonical_value_normalises_instants_and_orders_keys():
    ist = timezone(timedelta(hours=5, minutes=30))
    instant_ist = datetime(2026, 10, 5, 14, 30, tzinfo=ist)
    instant_utc = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    assert canonical_value(instant_ist) == canonical_value(instant_utc)
    assert canonical_json({"b": Decimal("1.50"), "a": date(2026, 10, 5)}) == '{"a":"2026-10-05","b":"1.50"}'


def test_schedule_with_bare_number_rate_raises(tmp_path):
    text = SCHEDULE_PATH.read_text(encoding="utf-8")
    assert '"delivery_rate": "0.0007"' in text
    broken = tmp_path / "bare_number.json"
    broken.write_text(text.replace('"delivery_rate": "0.0007"', '"delivery_rate": 0.0007'), encoding="utf-8")
    with pytest.raises(ScheduleError):
        load_schedule_set(broken)


def test_schedule_with_duplicate_key_raises(tmp_path):
    text = SCHEDULE_PATH.read_text(encoding="utf-8")
    broken = tmp_path / "duplicate_key.json"
    broken.write_text(
        text.replace('"segment": "cash",', '"segment": "cash",\n      "segment": "cash",', 1), encoding="utf-8"
    )
    with pytest.raises(ScheduleError):
        load_schedule_set(broken)


def test_committed_schedule_loads():
    schedule_set = load_schedule_set()
    assert len(schedule_set.versions) == 1
