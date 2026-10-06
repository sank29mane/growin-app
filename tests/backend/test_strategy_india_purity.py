"""AC-2: purity and no network (D-03). AST scan of backend/strategy_india.

Banned names are built from fragments so this file holds nothing GATE-02 would flag.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "backend" / "strategy_india"

BROKER_SDK = "_".join(("breeze", "connect"))
BANNED_ROOTS = {
    BROKER_SDK, "alpaca", "gateway", "execution", "simulation", "routes", "httpx", "requests", "socket",
    "urllib", "subprocess", "random", "uuid", "time",
}
HEAVY_ROOTS = {"numpy", "sklearn", "math", "statistics"}  # float maths: regime.py only
BANNED_CALLS = {"now", "utcnow", "today", "fromtimestamp", "utcfromtimestamp"}
FLOAT_ALLOWED = {"regime.py"}
CLOCK_ALLOWED = {"__main__.py"}
TICK_ADAPTER = "ticks.py"


def _int_like(node: ast.AST) -> bool:
    """An expression that is certainly an int: a literal, len(), int() or integer arithmetic on those."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in {"len", "int"}
    if isinstance(node, ast.UnaryOp):
        return _int_like(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.Pow)):
        return _int_like(node.left) and _int_like(node.right)
    return False


def scan_source(source: str, filename: str) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source, filename=filename)
    heavy_ok = filename in FLOAT_ALLOWED
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
            if node.module == "costs":
                modules += [f"costs.{alias.name}" for alias in node.names]
        for name in modules:
            root = name.split(".")[0]
            if root in BANNED_ROOTS:
                out.append(f"{filename}:{line}: banned import {name}")
            if root in HEAVY_ROOTS and not heavy_ok:
                out.append(f"{filename}:{line}: {name} is allowed only in regime.py")
            if name in ("costs.ticks",) and filename != TICK_ADAPTER:
                out.append(f"{filename}:{line}: costs.ticks may only be imported by ticks.py")
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in ("costs.ticks", "costs"):
            for alias in node.names:
                if alias.name.startswith("_") and (node.module == "costs.ticks" or alias.name == "_ticks"):
                    out.append(f"{filename}:{line}: private costs.ticks name {alias.name}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
            if isinstance(node.value, ast.Name) and node.value.id in ("costs_ticks", "_costs_ticks", "ticks"):
                out.append(f"{filename}:{line}: private costs.ticks name {node.attr}")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and not heavy_ok:
            if _int_like(node.left) and _int_like(node.right):
                out.append(f"{filename}:{line}: int/int true division yields a float; use Decimal")
        if isinstance(node, ast.Constant) and isinstance(node.value, float) and not heavy_ok:
            out.append(f"{filename}:{line}: float literal outside regime.py")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "float" and not heavy_ok:
                out.append(f"{filename}:{line}: float() outside regime.py")
            if isinstance(func, ast.Attribute) and func.attr in BANNED_CALLS and filename not in CLOCK_ALLOWED:
                out.append(f"{filename}:{line}: .{func.attr}() outside the CLI entry")
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS and filename not in CLOCK_ALLOWED:
                out.append(f"{filename}:{line}: {func.id}() outside the CLI entry")
    return out


def test_package_is_pure():
    files = sorted(PKG.glob("*.py"))
    assert len(files) >= 14, files
    violations: list[str] = []
    for path in files:
        violations += scan_source(path.read_text(encoding="utf-8"), path.name)
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source, filename, fragment",
    [
        (f"import {BROKER_SDK}", "x.py", "banned import"),
        ("from gateway import client", "x.py", "banned import"),
        ("from execution.ledger import canonical_json", "x.py", "banned import"),
        ("from simulation import engine", "x.py", "banned import"),
        ("from routes import market_routes", "x.py", "banned import"),
        ("import httpx", "x.py", "banned import"),
        ("import requests", "x.py", "banned import"),
        ("import socket", "x.py", "banned import"),
        ("import urllib.request", "x.py", "banned import"),
        ("import subprocess", "x.py", "banned import"),
        ("import numpy as np", "portfolio.py", "only in regime.py"),
        ("from sklearn.mixture import GaussianMixture", "engine.py", "only in regime.py"),
        ("x = 0.5", "metrics.py", "float literal"),
        ("import math", "metrics.py", "only in regime.py"),
        ("from statistics import NormalDist", "report.py", "only in regime.py"),
        ("x = 1 / 3", "hurdle.py", "int/int true division"),
        ("x = len(a) / 2", "portfolio.py", "int/int true division"),
        ("x = int(a) / len(b)", "portfolio.py", "int/int true division"),
        ("x = (len(a) + 1) / 4", "engine.py", "int/int true division"),
        ("x = float('1')", "engine.py", "float()"),
        ("import datetime\nx = datetime.datetime.now()", "engine.py", ".now()"),
        ("import datetime\nx = datetime.date.today()", "report.py", ".today()"),
        ("from costs.ticks import resolve_tick_from_table", "engine.py", "ticks.py"),
        ("from costs import ticks", "engine.py", "ticks.py"),
        ("from costs.ticks import _resolve_tick_from_table", "ticks.py", "private costs.ticks name"),
        ("import costs.ticks as _costs_ticks\nx = _costs_ticks._resolve_tick_from_table(t)", "ticks.py",
         "private costs.ticks name"),
    ],
)
def test_planted_violation_is_caught(source, filename, fragment):
    found = scan_source(source, filename)
    assert found and any(fragment in item for item in found), found


def test_decimal_division_is_not_flagged():
    ok = "from decimal import Decimal\nx = Decimal(1) / 3\ny = total / len(items)\nz = a // b\nw = sum(v) / len(v)\n"
    assert scan_source(ok, "metrics.py") == []


def test_allowed_places_stay_allowed():
    assert scan_source("import numpy as np\nimport math\nx = 0.5\ny = float(1)\nz = 1 / 3", "regime.py") == []
    assert scan_source("import datetime\nx = datetime.datetime.now()", "__main__.py") == []
    assert scan_source("from costs.ticks import load_tick_table", "ticks.py") == []


def test_only_the_tick_adapter_touches_costs_ticks():
    users = [p.name for p in PKG.glob("*.py") if "costs.ticks" in p.read_text() or "from costs import ticks" in p.read_text()]
    assert users == [TICK_ADAPTER]
