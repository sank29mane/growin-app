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
DYNAMIC_IMPORT_NAMES = {"importlib", "__import__", "import_module"}
IMPORT_RULE = "costs.ticks is reachable only as 'from costs.ticks import <public names>'"


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


def _strings(*nodes: ast.AST) -> list[str]:
    return [n.value for root in nodes for n in ast.walk(root) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _ticks_violation(node: ast.AST, filename: str, costs_names: set[str]) -> str | None:
    """Allowlist for costs.ticks: only `from costs.ticks import <public names>`, and only in the adapter.

    No module object can be bound that way, so aliasing, getattr, vars() and __dict__ on it cannot happen.
    Anything else that names the module, and any dynamic import machinery, is a violation.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] == "importlib":
                return "importlib is banned in strategy_india"
            if alias.name.startswith("costs.ticks"):
                return f"{IMPORT_RULE}, not import {alias.name}"
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        if node.module.split(".")[0] == "importlib":
            return "importlib is banned in strategy_india"
        if node.module == "costs" and any(alias.name in ("ticks", "*") for alias in node.names):
            return f"{IMPORT_RULE}, not 'from costs import ticks'"
        if node.module == "costs.ticks":
            if filename != TICK_ADAPTER:
                return "costs.ticks may only be imported by ticks.py"
            bad = [alias.name for alias in node.names if alias.name.startswith("_") or alias.name == "*"]
            if bad:
                return f"private costs.ticks name {', '.join(bad)}"
    elif isinstance(node, ast.Attribute):
        if node.attr in DYNAMIC_IMPORT_NAMES:
            return f"{node.attr} is banned in strategy_india"
        if node.attr == "ticks" and isinstance(node.value, ast.Name) and node.value.id in costs_names:
            return f"{IMPORT_RULE}, not a costs.ticks attribute chain"
    elif isinstance(node, ast.Name) and node.id in DYNAMIC_IMPORT_NAMES:
        return f"{node.id} is banned in strategy_india"
    elif isinstance(node, (ast.Call, ast.Subscript)):
        call = isinstance(node, ast.Call)
        parts = _strings(*node.args, *(k.value for k in node.keywords)) if call else _strings(node.slice)
        stripped = {part.strip(".") for part in parts}
        if any("costs.ticks" in part or part in DYNAMIC_IMPORT_NAMES for part in parts) or (
            call and {"costs", "ticks"} <= stripped
        ):
            return f"{IMPORT_RULE}, not a string naming the module"
    return None


def scan_source(source: str, filename: str) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source, filename=filename)
    costs_names = {"costs"} | {
        alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names if alias.name == "costs" and alias.asname
    }
    heavy_ok = filename in FLOAT_ALLOWED
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for name in modules:
            root = name.split(".")[0]
            if root in BANNED_ROOTS:
                out.append(f"{filename}:{line}: banned import {name}")
            if root in HEAVY_ROOTS and not heavy_ok:
                out.append(f"{filename}:{line}: {name} is allowed only in regime.py")
        problem = _ticks_violation(node, filename, costs_names)
        if problem:
            out.append(f"{filename}:{line}: {problem}")
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
        # costs.ticks: only public from-imports, only in ticks.py
        ("from costs.ticks import resolve_tick_from_table", "engine.py", "ticks.py"),
        ("from costs.ticks import _resolve_tick_from_table", "ticks.py", "private costs.ticks name"),
        ("from costs.ticks import resolve_nse_cash_tick, _tick_value", "ticks.py", "private costs.ticks name"),
        ("from costs.ticks import *", "ticks.py", "private costs.ticks name"),
        ("from costs.ticks import _x as public_looking", "ticks.py", "private costs.ticks name"),
        # the module bound or named any other way
        ("import costs.ticks", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct", "ticks.py", "not import costs.ticks"),
        ("from costs import ticks", "ticks.py", "not 'from costs import ticks'"),
        ("from costs import ticks as ct", "ticks.py", "not 'from costs import ticks'"),
        ("from costs import *", "ticks.py", "not 'from costs import ticks'"),
        ("from costs import ticks", "engine.py", "not 'from costs import ticks'"),
        ("import costs\nx = costs.ticks.committed_tick_table(c)", "ticks.py", "attribute chain"),
        ("import costs\nt = costs.ticks", "ticks.py", "attribute chain"),
        ("import costs as c\nt = c.ticks", "ticks.py", "attribute chain"),
        ("import costs.ticks\nx = costs.ticks._resolve_tick_from_table(t)", "ticks.py", "attribute chain"),
        # aliasing and reflection: the import that makes them possible is itself the violation
        ("import costs.ticks as ct\nt = ct\nx = t._resolve_tick_from_table(a)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = getattr(ct, name)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = getattr(ct, name=n)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = hasattr(ct, name=n)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nsetattr(ct, name=n, value=v)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\ndelattr(ct, n)", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\ndelattr(ct, name=n)", "ticks.py", "not import costs.ticks"),
        ("from costs import ticks as ct\nx = getattr(ct, '_resolve_tick_from_table')", "ticks.py", "not 'from costs"),
        ("from costs import ticks as ct\nx = hasattr(ct, '_resolve_tick_from_table')", "ticks.py", "not 'from costs"),
        ("import costs.ticks as ct\nx = getattr(ct, '__dict__')[k]", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = ct.__dict__[name]", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = vars(ct)['_resolve_tick_from_table']", "ticks.py", "not import costs.ticks"),
        ("import costs.ticks as ct\nx = vars(object=ct)[k]", "ticks.py", "not import costs.ticks"),
        ("import costs\nx = getattr(costs.ticks, name=n)", "ticks.py", "attribute chain"),
        ("import costs\nx = vars(object=costs.ticks)", "ticks.py", "attribute chain"),
        # strings naming the module, and any dynamic import machinery
        ("import sys\nm = sys.modules['costs.ticks']", "ticks.py", "string naming the module"),
        ("m = lookup('costs.ticks')", "ticks.py", "string naming the module"),
        ("m = lookup('ticks', 'costs')", "ticks.py", "string naming the module"),
        ("m = lookup('.ticks', package='costs')", "ticks.py", "string naming the module"),
        ("import importlib", "ticks.py", "importlib is banned"),
        ("import importlib.util", "ticks.py", "importlib is banned"),
        ("from importlib import util", "ticks.py", "importlib is banned"),
        ("import importlib\nm = importlib.import_module('costs.ticks')", "ticks.py", "importlib is banned"),
        ("import importlib\nm = importlib.import_module('costs.ticks')", "ticks.py", "string naming the module"),
        ("import importlib\nm = importlib.import_module('ticks', 'costs')", "ticks.py", "string naming the module"),
        ("import importlib\nm = importlib.import_module('.ticks', package='costs')", "ticks.py",
         "string naming the module"),
        ("import importlib\nm = importlib.import_module(name=n)", "ticks.py", "import_module is banned"),
        ("import importlib\nm = importlib.import_module(name)", "ticks.py", "import_module is banned"),
        ("from importlib import import_module\nm = import_module('costs.ticks')", "ticks.py", "importlib is banned"),
        ("from importlib import import_module as im\nm = im('costs.ticks')", "ticks.py", "importlib is banned"),
        ("from importlib import import_module as im\nm = im('costs.ticks')", "ticks.py", "string naming the module"),
        ("m = __import__('costs.ticks')", "ticks.py", "__import__ is banned"),
        ("m = __import__('costs', fromlist=['ticks'])", "ticks.py", "string naming the module"),
        ("m = __import__(name)", "ticks.py", "__import__ is banned"),
        ("import builtins\nm = getattr(builtins, '__import__')(n)", "ticks.py", "string naming the module"),
    ],
)
def test_planted_violation_is_caught(source, filename, fragment):
    found = scan_source(source, filename)
    assert found and any(fragment in item for item in found), found


def test_decimal_division_is_not_flagged():
    ok = "from decimal import Decimal\nx = Decimal(1) / 3\ny = total / len(items)\nz = a // b\nw = sum(v) / len(v)\n"
    assert scan_source(ok, "metrics.py") == []


def test_public_from_import_of_costs_ticks_stays_allowed():
    ok = ("from costs.ticks import resolve_nse_cash_tick\n"
          "from costs.ticks import InstrumentClass, TickTable, committed_tick_table\n"
          "from costs.ticks import align_limit as _align_limit\n"
          "a = resolve_nse_cash_tick(session_date=d)\nb = _align_limit(p, t, s)\n")
    assert scan_source(ok, "ticks.py") == []
    assert scan_source("from costs.ticks import resolve_nse_cash_tick", "ticks.py") == []


def test_unrelated_reflection_and_attributes_stay_allowed():
    ok = ("from costs.core import Side\nfrom costs import schedule\nimport costs.core\n"
          "d = self._cache\ne = other._hidden\nf = row.ticks\ng = getattr(row, name)\nh = hasattr(self, name)\n"
          "i = vars(row)\nj = row.__dict__['k']\nk = getattr(row, 'align_limit', None)\nl = costs.core.IST\n"
          "m = fmt('tick table for {}', cls)\n")
    assert scan_source(ok, "engine.py") == []


def test_allowed_places_stay_allowed():
    assert scan_source("import numpy as np\nimport math\nx = 0.5\ny = float(1)\nz = 1 / 3", "regime.py") == []
    assert scan_source("import datetime\nx = datetime.datetime.now()", "__main__.py") == []
    assert scan_source("from costs.ticks import load_tick_table", "ticks.py") == []


def test_only_the_tick_adapter_touches_costs_ticks():
    users = [p.name for p in PKG.glob("*.py") if "costs.ticks" in p.read_text()]
    assert users == [TICK_ADAPTER]
