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
EVAL_NAMES = {"exec", "eval", "compile"}
SYS_MODULES_RULE = "sys.modules is banned in strategy_india: any lookup reaches a banned module without an import"
SYS_FILES = {"__main__.py"}  # the only file that may name `sys`
SYS_ATTRS_ALLOWED = {"argv", "exit", "stdout", "stderr", "stdin"}
REFLECTION_NAMES = {"globals", "locals", "vars"}
REFLECTION_ATTRS = {"__dict__", "__globals__"}
SYS_IMPORT_RULE = "sys may only be imported (plainly, as 'import sys') by __main__.py"
SYS_USE_RULE = "the bare name sys may only appear as sys.argv, sys.exit, sys.stdout, sys.stderr or sys.stdin"
REFLECTION_RULE = "globals/locals/vars and __dict__/__globals__ are banned in strategy_india: they reach sys and banned modules"
IMPORT_RULE = "costs.ticks is reachable only as 'from costs.ticks import <public names>'"


def _norm(name: str) -> str:
    """Strip one leading `backend.`: tests/backend/conftest.py lets the runtime import `backend.<pkg>`."""
    return name[len("backend."):] if name.startswith("backend.") else name


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

    Imported names must be public; local `as _foo` aliases remain allowed.
    No module object can be bound that way, so aliasing, getattr, vars() and __dict__ on it cannot happen.
    Anything else that names the module, and any dynamic import machinery, is a violation.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            name = _norm(alias.name)
            if name == "backend":
                return "bare 'import backend' is banned in strategy_india"
            if name.split(".")[0] == "importlib":
                return "importlib is banned in strategy_india"
            if name.startswith("costs.ticks"):
                return f"{IMPORT_RULE}, not import {alias.name}"
            if name.split(".")[0] == "costs":
                return f"costs is reachable only through 'from costs.<submodule> import <names>', not import {alias.name}"
    elif isinstance(node, ast.ImportFrom):
        if node.level >= 2:
            return "relative import leaves strategy_india"
        if any(alias.name == "costs" for alias in node.names):
            return "costs may not be imported as a name: use 'from costs.<submodule> import <names>'"
        if node.level != 0 or not node.module:
            return None
        module = _norm(node.module)
        if module.split(".")[0] == "importlib":
            return "importlib is banned in strategy_india"
        if module == "costs" and any(alias.name in ("ticks", "*") for alias in node.names):
            return f"{IMPORT_RULE}, not 'from costs import ticks'"
        if module == "costs.ticks":
            if filename != TICK_ADAPTER:
                return "costs.ticks may only be imported by ticks.py"
            bad = [alias.name for alias in node.names if alias.name.startswith("_") or alias.name == "*"]
            if bad:
                return f"private costs.ticks name {', '.join(bad)}"
    elif isinstance(node, ast.Attribute):
        if node.attr in DYNAMIC_IMPORT_NAMES:
            return f"{node.attr} is banned in strategy_india"
        if node.attr in {"exec", "eval"} or (
            node.attr == "compile" and isinstance(node.value, ast.Name) and node.value.id in {"builtins", "__builtins__"}
        ):
            return f"{node.attr} is banned in strategy_india"
        if node.attr == "ticks" and isinstance(node.value, ast.Name) and node.value.id in costs_names:
            return f"{IMPORT_RULE}, not a costs.ticks attribute chain"
    elif isinstance(node, ast.Name) and (node.id in DYNAMIC_IMPORT_NAMES or node.id in EVAL_NAMES):
        return f"{node.id} is banned in strategy_india"
    elif isinstance(node, (ast.Call, ast.Subscript)):
        call = isinstance(node, ast.Call)
        if call and isinstance(node.func, ast.Name) and node.func.id == "getattr" and any(
            part in EVAL_NAMES for part in _strings(*node.args)
        ):
            return "exec/eval/compile is banned in strategy_india"
        parts = [_norm(part) for part in (
            _strings(*node.args, *(k.value for k in node.keywords)) if call else _strings(node.slice)
        )]
        stripped = {_norm(part.strip(".")) for part in parts}
        if any("costs.ticks" in part or part in DYNAMIC_IMPORT_NAMES for part in parts) or (
            call and {"costs", "ticks"} <= stripped
        ):
            return f"{IMPORT_RULE}, not a string naming the module"
    return None


def _binding_violation(node: ast.AST, sys_names: set[str]) -> str | None:
    """Ways to reach a banned module without a from-import that the other rules can see."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "sys.modules" or alias.name.startswith("sys.modules."):
                return SYS_MODULES_RULE
            if alias.name.startswith("backend."):
                return f"plain 'import {alias.name}' binds the name backend: use from-imports"
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "backend":
        if any(alias.name == "*" for alias in node.names):
            return "'from backend import *' binds every backend package: use named from-imports"
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "sys":
        if any(alias.name in ("modules", "*") for alias in node.names):
            return SYS_MODULES_RULE
    elif isinstance(node, ast.Attribute) and node.attr == "modules":
        if isinstance(node.value, ast.Name) and node.value.id in sys_names:
            return SYS_MODULES_RULE
    return None


def _sys_violation(node: ast.AST, filename: str, sanctioned: set[int]) -> str | None:
    """Structural sys rule: no import outside __main__.py; there, only direct reads of an allowlisted attribute."""
    in_main = filename in SYS_FILES
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "sys" or alias.name.startswith("sys."):
                if not in_main or alias.name != "sys" or alias.asname:
                    return SYS_IMPORT_RULE
    elif isinstance(node, ast.ImportFrom):
        if node.level == 0 and node.module and (node.module == "sys" or node.module.startswith("sys.")):
            return "'from sys import ...' is banned in strategy_india"
        if any(alias.name == "sys" for alias in node.names):
            return "'from <module> import sys' binds the sys module: banned in strategy_india"
    elif isinstance(node, ast.Attribute) and node.attr == "sys":
        return "an attribute named sys (for example os.sys) reaches the sys module: banned in strategy_india"
    elif isinstance(node, ast.Name) and node.id == "sys":
        if not in_main:
            return SYS_IMPORT_RULE
        if id(node) not in sanctioned:
            return SYS_USE_RULE
    elif isinstance(node, (ast.Global, ast.Nonlocal)) and "sys" in node.names:
        return SYS_USE_RULE
    elif isinstance(node, ast.Constant) and node.value == "sys":
        return "the string 'sys' (getattr/import by name) is banned in strategy_india"
    return None


def _reflection_violation(node: ast.AST) -> str | None:
    """globals/locals/vars and .__dict__ hand back the namespace that holds sys: ban them package-wide."""
    if isinstance(node, ast.Name) and node.id in REFLECTION_NAMES:
        return REFLECTION_RULE
    if isinstance(node, ast.Attribute):
        if node.attr in REFLECTION_ATTRS:
            return REFLECTION_RULE
        if node.attr in REFLECTION_NAMES and isinstance(node.value, ast.Name) and node.value.id in {"builtins", "__builtins__"}:
            return REFLECTION_RULE
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in REFLECTION_NAMES | REFLECTION_ATTRS:
        return REFLECTION_RULE  # getattr(x, '__dict__'), getattr(__builtins__, 'vars')
    return None


def _not_public(name: str) -> bool:
    """Only plain public identifiers pass a from-import allowlist: no underscore prefix, so no dunders either.

    `from pilot_data.dataset import __dict__` binds the module dict, which holds the private readers.
    """
    return name.startswith("_")


PILOT_RULE = "pilot_data is reachable only as 'from pilot_data.<module> import <public names>'"


def _pilot_data_violation(node: ast.AST) -> str | None:
    """Allowlist for pilot_data: only `from pilot_data.<module> import <public names>`.

    No module object can be bound that way, so aliasing, getattr, vars(), __dict__ and __getattribute__
    on it cannot happen. Everything else that names the package is a violation.
    """
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.removeprefix("backend.").split(".")[0] == "pilot_data":
                return f"{PILOT_RULE}, not import {alias.name}"
    elif isinstance(node, ast.ImportFrom):
        if any(alias.name == "pilot_data" for alias in node.names):
            return f"{PILOT_RULE}, not pilot_data imported as a name"
        if node.level != 0 or not node.module:
            return None
        parts = node.module.removeprefix("backend.").split(".")
        if parts[0] != "pilot_data":
            return None
        if len(parts) == 1:
            return f"{PILOT_RULE}, not 'from {node.module} import ...' (that binds a module)"
        if any(_not_public(part) for part in parts):
            return f"{PILOT_RULE}, not a private module in 'from {node.module} import ...'"
        bad = [alias.name for alias in node.names if _not_public(alias.name) or alias.name == "*"]
        if bad:
            return f"private pilot_data name {', '.join(bad)}"
    elif isinstance(node, ast.Attribute):
        root = node.value
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id == "pilot_data":
            return f"{PILOT_RULE}, not a pilot_data attribute chain"
    elif isinstance(node, (ast.Call, ast.Subscript)):
        call = isinstance(node, ast.Call)
        parts = _strings(*node.args, *(k.value for k in node.keywords)) if call else _strings(node.slice)
        if any(_norm(part.strip(".")).split(".")[0] == "pilot_data" for part in parts):
            return f"{PILOT_RULE}, not a string naming the package"
    return None


def scan_source(source: str, filename: str) -> list[str]:
    out: list[str] = []
    tree = ast.parse(source, filename=filename)
    costs_names = {"costs"} | {
        alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names if _norm(alias.name) == "costs" and alias.asname
    }
    sys_names = {"sys"} | {
        alias.asname for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names if alias.name == "sys" and alias.asname
    }
    sanctioned = {
        id(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in SYS_ATTRS_ALLOWED and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name) and node.value.id == "sys"
    }
    heavy_ok = filename in FLOAT_ALLOWED
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]  # relative imports of level 1 stay in the package; level >= 2 is banned below
            if node.module == "backend":
                modules += [f"backend.{alias.name}" for alias in node.names]  # `from backend import execution`
        for name in modules:
            root = _norm(name).split(".")[0]
            if root in BANNED_ROOTS:
                out.append(f"{filename}:{line}: banned import {name}")
            if root in HEAVY_ROOTS and not heavy_ok:
                out.append(f"{filename}:{line}: {name} is allowed only in regime.py")
        problem = _ticks_violation(node, filename, costs_names)
        if problem:
            out.append(f"{filename}:{line}: {problem}")
        problem = _binding_violation(node, sys_names)
        if problem:
            out.append(f"{filename}:{line}: {problem}")
        problem = _sys_violation(node, filename, sanctioned)
        if problem:
            out.append(f"{filename}:{line}: {problem}")
        problem = _reflection_violation(node)
        if problem:
            out.append(f"{filename}:{line}: {problem}")
        problem = _pilot_data_violation(node)
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
        ("from costs.ticks import __dict__", "ticks.py", "private costs.ticks name"),
        ("from costs.ticks import __dict__ as d", "ticks.py", "private costs.ticks name"),
        ("import costs.ticks as ct\nx = vars(object=ct)[k]", "ticks.py", "not import costs.ticks"),
        ("import costs\nx = getattr(costs.ticks, name=n)", "ticks.py", "attribute chain"),
        ("import costs\nx = vars(object=costs.ticks)", "ticks.py", "attribute chain"),
        # strings naming the module, and any dynamic import machinery
        ("import sys\nm = sys.modules['costs.ticks']", "ticks.py", "string naming the module"),
        ("m = lookup('costs.ticks')", "ticks.py", "string naming the module"),
        ("m = lookup('ticks', 'costs')", "ticks.py", "string naming the module"),
        ("m = lookup('.ticks', package='costs')", "ticks.py", "string naming the module"),
        ("m = lookup('backend.costs', 'ticks')", "ticks.py", "string naming the module"),
        ("m = lookup('.ticks', package='backend.costs')", "ticks.py", "string naming the module"),
        ("m = lookup('ticks', package='backend.costs')", "ticks.py", "string naming the module"),
        ("import sys\nm = sys.modules['backend.costs.ticks']", "ticks.py", "string naming the module"),
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
        # relative imports that leave the package
        ("from ..costs.ticks import _x", "ticks.py", "relative import leaves"),
        ("from ..costs import ticks", "ticks.py", "relative import leaves"),
        ("from .. import costs as c\nx = c.ticks", "ticks.py", "relative import leaves"),
        ("from ... import costs", "ticks.py", "relative import leaves"),
        ("from ..costs.core import Side", "engine.py", "relative import leaves"),
        ("from .. import gateway", "engine.py", "relative import leaves"),
        # bare costs, in every form
        ("import costs", "engine.py", "not import costs"),
        ("import costs as c", "engine.py", "not import costs"),
        ("import costs.core", "engine.py", "not import costs.core"),
        ("import costs.core as cc", "engine.py", "not import costs.core"),
        ("import costs\nx = getattr(costs, 'ticks')", "engine.py", "not import costs"),
        ("import costs\nx = vars(costs)['ticks']", "engine.py", "not import costs"),
        ("import costs\nx = costs.__dict__['ticks']", "engine.py", "not import costs"),
        ("import costs\nx = costs.__getattribute__('ticks')", "engine.py", "not import costs"),
        ("from . import costs", "engine.py", "may not be imported as a name"),
        ("from .. import costs", "engine.py", "relative import leaves"),
        ("from backend import costs as c", "engine.py", "may not be imported as a name"),
        ("from costs import costs", "engine.py", "may not be imported as a name"),
        # a leading `backend.` is the same module: every rule applies to it too
        ("from backend.costs.ticks import resolve_tick_from_table", "engine.py", "ticks.py"),
        ("from backend.costs.ticks import _resolve_tick_from_table", "ticks.py", "private costs.ticks name"),
        ("from backend.costs import ticks", "ticks.py", "not 'from costs import ticks'"),
        ("from backend.costs import ticks", "engine.py", "not 'from costs import ticks'"),
        ("import backend.costs.ticks as ct", "ticks.py", "not import backend.costs.ticks"),
        ("import backend.costs.ticks", "ticks.py", "not import backend.costs.ticks"),
        ("import backend.costs as c", "engine.py", "not import backend.costs"),
        ("import backend.costs as c\nx = c.ticks._x", "ticks.py", "attribute chain"),
        ("import backend", "engine.py", "bare 'import backend' is banned"),
        ("import backend as b", "engine.py", "bare 'import backend' is banned"),
        ("from backend.execution import X", "engine.py", "banned import backend.execution"),
        ("from backend.execution.ledger import canonical_json", "engine.py", "banned import"),
        ("import backend.execution", "engine.py", "banned import backend.execution"),
        ("from backend import execution", "engine.py", "banned import backend.execution"),
        ("from backend.gateway import client", "engine.py", "banned import"),
        ("from backend.importlib import util", "engine.py", "importlib is banned"),
        ("from backend.statistics import NormalDist", "report.py", "only in regime.py"),
        # plain `import backend.X` binds the name backend, so backend.execution.f() would scan clean
        ("import backend.utils", "engine.py", "plain 'import backend.utils' binds"),
        ("import backend.utils\nbackend.execution.f()", "engine.py", "plain 'import backend.utils' binds"),
        ("import backend.strategy_india.engine", "engine.py", "plain 'import backend.strategy_india.engine' binds"),
        ("import backend.utils as u", "engine.py", "plain 'import backend.utils' binds"),
        ("import os, backend.utils", "engine.py", "plain 'import backend.utils' binds"),
        ("from backend import *", "engine.py", "'from backend import *'"),
        ("from backend import *\nexecution.f()", "engine.py", "'from backend import *'"),
        # sys.modules is banned outright: subscripts, .get(), aliases and from-imports
        ("import sys\nx = sys.modules['costs'].ticks.f", "ticks.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['backend.costs'].ticks", "ticks.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['costs.ticks']._x", "ticks.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['backend.costs.ticks'].f", "ticks.py", "sys.modules is banned"),
        ("import sys as s\nx = s.modules['execution'].f()", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['backend.execution.ledger']", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['gateway']", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules['backend'].execution", "engine.py", "sys.modules is banned"),
        ("from sys import modules\nx = modules['time']", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules.get('backend.costs').ticks", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules.get('backend.execution')", "engine.py", "sys.modules is banned"),
        ("from sys import modules as loaded\nx = loaded['backend.execution']", "engine.py", "sys.modules is banned"),
        ("import sys as s\nx = s.modules['costs.ticks']", "engine.py", "sys.modules is banned"),
        ("import sys as s\nx = s.modules.get(name)", "engine.py", "sys.modules is banned"),
        ("import sys\nx = sys.modules", "engine.py", "sys.modules is banned"),
        ("from sys import modules", "engine.py", "sys.modules is banned"),
        ("from sys import *", "engine.py", "sys.modules is banned"),
        ("import sys.modules", "engine.py", "sys.modules is banned"),
        # sys, structurally: no import outside __main__.py (every Codex bypass form dies at the import)
        ("import sys", "engine.py", "sys may only be imported"),
        ("import sys as s", "engine.py", "sys may only be imported"),
        ("import os, sys", "engine.py", "sys may only be imported"),
        ("import sys.modules", "engine.py", "sys may only be imported"),
        ("from sys import argv", "engine.py", "'from sys import ...' is banned"),
        ("from sys import modules", "__main__.py", "'from sys import ...' is banned"),
        ("from sys import exit as leave", "__main__.py", "'from sys import ...' is banned"),
        ("from os import sys", "engine.py", "'from <module> import sys'"),
        ("from os import sys as s", "__main__.py", "'from <module> import sys'"),
        ("import os\nx = os.sys", "engine.py", "an attribute named sys"),
        ("import os\nx = os.sys.modules", "__main__.py", "an attribute named sys"),
        ("x = getattr(os, 'sys')", "engine.py", "the string 'sys'"),
        ("import sys\nm = globals()['sys'].modules['costs.ticks']", "engine.py", "sys may only be imported"),
        ("import sys\nm = getattr(sys, 'modules')['costs.ticks']", "engine.py", "sys may only be imported"),
        ("import sys\nm = vars(sys)['modules']", "engine.py", "sys may only be imported"),
        ("import sys\nm = sys.__dict__['modules']", "engine.py", "sys may only be imported"),
        ("import sys\ns = sys\nm = s.modules['costs.ticks']", "engine.py", "sys may only be imported"),
        ("x = f(sys)", "engine.py", "sys may only be imported"),  # name reached without an import statement here
        ("x = sys.argv", "engine.py", "sys may only be imported"),
        # in __main__.py: the bare name sys only as a direct read of argv/exit/stdout/stderr/stdin
        ("import sys\nm = globals()['sys'].modules['costs.ticks']", "__main__.py", "globals/locals/vars"),
        ("import sys\nm = getattr(sys, 'modules')['costs.ticks']", "__main__.py", "the bare name sys may only"),
        ("import sys\nm = vars(sys)['modules']", "__main__.py", "the bare name sys may only"),
        ("import sys\nm = sys.__dict__['modules']", "__main__.py", "globals/locals/vars"),
        ("import sys\ns = sys\nm = s.modules['costs.ticks']", "__main__.py", "the bare name sys may only"),
        ("import sys\nm = sys.modules['costs.ticks']", "__main__.py", "sys.modules is banned"),
        ("import sys\nm = sys.modules", "__main__.py", "the bare name sys may only"),
        ("import sys\nm = sys.path", "__main__.py", "the bare name sys may only"),
        ("import sys\nf(sys)", "__main__.py", "the bare name sys may only"),
        ("import sys\nf(x=sys)", "__main__.py", "the bare name sys may only"),
        ("import sys\nx = sys['modules']", "__main__.py", "the bare name sys may only"),
        ("import sys\nx = [sys][0].modules", "__main__.py", "the bare name sys may only"),
        ("import sys\nx = {'k': sys}", "__main__.py", "the bare name sys may only"),
        ("import sys\nx = getattr(sys, name)", "__main__.py", "the bare name sys may only"),
        ("import sys\nsys = other", "__main__.py", "the bare name sys may only"),
        ("import sys\nsys.exit = other", "__main__.py", "the bare name sys may only"),
        ("import sys\ndel sys", "__main__.py", "the bare name sys may only"),
        ("import sys\nwith open(p) as sys:\n    pass", "__main__.py", "the bare name sys may only"),
        ("import sys\ndef f():\n    global sys", "__main__.py", "the bare name sys may only"),
        ("import sys as s\nx = s.argv", "__main__.py", "sys may only be imported"),
        ("import sys.modules", "__main__.py", "sys may only be imported"),
        ("import os, sys as s", "__main__.py", "sys may only be imported"),
        # globals/locals/vars and __dict__/__globals__, package-wide and without exceptions
        ("x = globals()", "engine.py", "globals/locals/vars"),
        ("x = globals()['k']", "__main__.py", "globals/locals/vars"),
        ("x = locals()", "engine.py", "globals/locals/vars"),
        ("x = vars(row)", "engine.py", "globals/locals/vars"),
        ("x = vars()", "metrics.py", "globals/locals/vars"),
        ("x = vars(object=row)", "metrics.py", "globals/locals/vars"),
        ("g = globals\nx = g()", "engine.py", "globals/locals/vars"),
        ("import builtins\nx = builtins.vars(row)", "engine.py", "globals/locals/vars"),
        ("x = __builtins__.globals()", "engine.py", "globals/locals/vars"),
        ("x = getattr(__builtins__, 'vars')(row)", "engine.py", "globals/locals/vars"),
        ("x = row.__dict__", "report.py", "globals/locals/vars"),
        ("x = row.__dict__.items()", "report.py", "globals/locals/vars"),
        ("x = row.__dict__['k']", "engine.py", "globals/locals/vars"),
        ("x = getattr(row, '__dict__')", "engine.py", "globals/locals/vars"),
        ("x = hasattr(row, '__dict__')", "engine.py", "globals/locals/vars"),
        ("x = type(row).__dict__", "engine.py", "globals/locals/vars"),
        ("x = f.__globals__['sys']", "engine.py", "globals/locals/vars"),
        ("x = getattr(f, '__globals__')", "engine.py", "globals/locals/vars"),
        # Phase 59: only 'from pilot_data.<module> import <public names>'; no module object can be bound
        ("import pilot_data", "data.py", "not import pilot_data"),
        ("import pilot_data as pd", "data.py", "not import pilot_data"),
        ("import pilot_data.dataset", "data.py", "not import pilot_data.dataset"),
        ("import pilot_data.dataset as d\nrows = d._read_parquet(p)", "data.py", "not import pilot_data.dataset"),
        ("import backend.pilot_data.dataset as d", "data.py", "not import backend.pilot_data.dataset"),
        ("import pilot_data._internal", "data.py", "not import pilot_data._internal"),
        ("from pilot_data import dataset", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nrows = d._read_parquet(p)", "data.py", "that binds a module"),
        ("from pilot_data import *", "data.py", "that binds a module"),
        ("from backend.pilot_data import dataset", "data.py", "that binds a module"),
        ("from backend import pilot_data", "data.py", "imported as a name"),
        ("from . import pilot_data", "data.py", "imported as a name"),
        ("from .. import pilot_data", "data.py", "imported as a name"),
        ("from pilot_data._internal import thing", "data.py", "private module"),
        ("from pilot_data.dataset import _read_parquet", "data.py", "private pilot_data name"),
        ("from pilot_data.dataset import _read_parquet as read", "data.py", "private pilot_data name"),
        ("from pilot_data.dataset import *", "data.py", "private pilot_data name"),
        ("from backend.pilot_data.dataset import _read_parquet", "data.py", "private pilot_data name"),
        # dunders bind the module dict or loader, so they are as forbidden as single-underscore names
        ("from pilot_data.dataset import __dict__", "data.py", "private pilot_data name"),
        ("from pilot_data.dataset import __loader__", "data.py", "private pilot_data name"),
        # Grok's reflection forms: each needs the module object, which the allowlist never lets anyone bind
        ("from pilot_data import dataset as d\nf = getattr(d, '_read_parquet')", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nf = getattr(d, name='_read_parquet')", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nf = hasattr(d, name='_read_parquet')", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nf = vars(d)['_read_parquet']", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nf = d.__dict__['_read_parquet']", "data.py", "that binds a module"),
        ("from pilot_data import dataset as d\nf = d.__getattribute__('_read_parquet')", "data.py", "that binds a module"),
        ("import pilot_data.dataset as d\nf = d.__getattribute__('_read_parquet')", "data.py", "not import pilot_data.dataset"),
        # the package named without an import statement
        ("x = pilot_data.dataset.read_dataset_rows(p)", "data.py", "attribute chain"),
        ("x = pilot_data.dataset._read_parquet(p)", "data.py", "attribute chain"),
        ("f = lookup('pilot_data.dataset')", "data.py", "string naming the package"),
        ("f = lookup(name='pilot_data')", "data.py", "string naming the package"),
        ("f = lookup('backend.pilot_data')", "data.py", "string naming the package"),
        ("f = lookup('backend.pilot_data.dataset')", "data.py", "string naming the package"),
        ("import importlib\nm = importlib.import_module('pilot_data.dataset')", "data.py", "string naming the package"),
        # exec, eval, compile
        ("exec('x = 1')", "engine.py", "exec is banned"),
        ("x = eval(s)", "engine.py", "eval is banned"),
        ("c = compile(s, 'f', 'exec')", "engine.py", "compile is banned"),
        ("f = eval\nx = f(s)", "engine.py", "eval is banned"),
        ("import builtins\nbuiltins.exec(s)", "engine.py", "exec is banned"),
        ("import builtins\nx = builtins.eval(s)", "engine.py", "eval is banned"),
        ("import builtins\nc = builtins.compile(s, 'f', 'exec')", "engine.py", "compile is banned"),
        ("x = getattr(__builtins__, 'eval')(s)", "engine.py", "exec/eval/compile is banned"),
    ],
)
def test_planted_violation_is_caught(source, filename, fragment):
    found = scan_source(source, filename)
    assert found and any(fragment in item for item in found), found


def test_from_imports_of_allowed_packages_stay_allowed():
    ok = ("from backend.utils import helper\nfrom backend import utils\nfrom backend.strategy_india import engine\n"
          "e = other.modules[0]\nf = other.sys_like\n")
    assert scan_source(ok, "engine.py") == []


def test_sys_in_main_is_allowed_only_as_a_direct_read_of_the_allowlist():
    ok = ("import sys\nimport os\n"
          "a = sys.argv[1:]\nb = sys.stdout.write('x')\nc = sys.stderr\nd = sys.stdin.read()\n"
          "def main(argv=None):\n    return 0\n"
          "sys.exit(main())\n")
    assert scan_source(ok, "__main__.py") == []


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
    ok = ("from costs.core import Side\nfrom costs import schedule\nfrom .errors import X\nfrom . import metrics\n"
          "import re\nr = re.compile('a')\n"
          "d = self._cache\ne = other._hidden\nf = row.ticks\ng = getattr(row, name)\nh = hasattr(self, name)\n"
          "k = getattr(row, 'align_limit', None)\nl = core.IST\n"
          "m = fmt('tick table for {}', cls)\n")
    assert scan_source(ok, "engine.py") == []


def test_allowed_places_stay_allowed():
    assert scan_source("import numpy as np\nimport math\nx = 0.5\ny = float(1)\nz = 1 / 3", "regime.py") == []
    assert scan_source("import datetime\nx = datetime.datetime.now()", "__main__.py") == []
    assert scan_source("from costs.ticks import load_tick_table", "ticks.py") == []


def _imports_ticks(source: str) -> bool:
    """True if the source imports costs.ticks in any form, with or without a `backend.` prefix."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(_norm(a.name).startswith("costs.ticks") for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = _norm(node.module)
            if module.startswith("costs.ticks") or (module == "costs" and any(a.name == "ticks" for a in node.names)):
                return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "from costs.ticks import x",
        "from backend.costs.ticks import x",
        "from backend.costs import ticks",
        "from costs import ticks",
        "import backend.costs.ticks as ct",
    ],
)
def test_adapter_detector_sees_every_form(source):
    assert _imports_ticks(source)


def test_adapter_detector_ignores_unrelated_imports():
    assert not _imports_ticks("from costs.core import Side\nfrom backend.costs import schedule")
def test_public_pilot_data_names_stay_allowed():
    ok = ("from pilot_data.dataset import read_dataset_rows, verify_dataset\n"
          "from pilot_data.core import canonical_sha256 as _sha, PilotDataError\n"
          "from pilot_data.surveillance import snapshot_for\n"
          "rows = read_dataset_rows(p)\nm = verify_dataset(p, workspace='india')\nh = _sha(rows)\n"
          "y = other._private_thing\nz = getattr(other, '_x')\nw = verify_dataset.__name__\n")
    assert scan_source(ok, "data.py") == []


def test_parameter_shadowing_a_from_imported_name_is_not_a_violation():
    # vars() and __dict__ stay out: the reflection rule bans them package-wide, shadowed or not
    ok = ("from pilot_data.core import PilotDataError, canonical_sha256\n"
          "def handle(err):\n    return err._code, err._detail\n"
          "def hash_it(canonical_sha256):\n    return canonical_sha256._cache\n"
          "def wrap(PilotDataError):\n    return getattr(PilotDataError, '_code'), PilotDataError._hint\n"
          "try:\n    pass\nexcept PilotDataError as err:\n    code = err._code\n")
    assert scan_source(ok, "data.py") == []


def test_only_the_tick_adapter_touches_costs_ticks():
    users = [
        p.name for p in PKG.glob("*.py")
        if "costs.ticks" in p.read_text(encoding="utf-8") or _imports_ticks(p.read_text(encoding="utf-8"))
    ]
    assert users == [TICK_ADAPTER]
