"""Phase 64-03 Task 1: purity and import rules for ``backend/shadow_india`` (P-10, T-64-12, D-05).

An AST scan, not a grep. Banned names are built from fragments so this file holds nothing GATE-02 would flag.
Rules:

1. No module imports a Breeze SDK or any gateway code (the Mac holds no broker client, and the evidence anchor
   rules are mirrored, not imported).
2. No module touches the network: sockets, HTTP clients, urllib, websockets. The VM leg goes through 63-05's
   ``RelayClient`` in ``runner.py``, which this package never reimplements.
3. ``backend/execution/*`` is imported only by ``admission.py``, ``runner.py`` and ``runtime.py`` (the edge that builds the
   production runtime, 64-09 Task B).
4. Pure modules (everything but ``admission.py``, ``runner.py``, ``runtime.py`` and ``__main__.py``) have no subprocess, no
   randomness, no dynamic import or eval, no ``sys.modules``; ``policy.py`` also has no clock at all.
5. ``vm_replay.py`` (64-05 Task 2b) alone may run the recorded VM build's source bytes. The exemption is not a
   module-wide switch. Two named top-level functions, ``_exec_source`` (``compile``, ``exec``, one ``sys.modules``
   registration) and ``_forget_package`` (the ``sys.modules`` cleanup), are pinned by their complete text
   (signature, decorators and body, compared as ``ast.unparse`` with the docstring left out), each must be defined
   exactly once at module top level, and the one ``gateway_vm`` directory-name constant is pinned the same way. A
   function that differs by a token, a duplicate of one, a repeated or moved statement, a second loader in any other
   function, ``importlib`` anywhere and every other rule (no network, no relay client, no ``execution`` import, no
   static gateway import, no ``import_module``, ``__import__``, subprocess or randomness) are flagged as usual.

Plants below are regression cases for the scanner itself.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "backend" / "shadow_india"

BROKER_SDK = "_".join(("breeze", "connect"))
GATEWAY_ROOTS = {"gateway", "gateway_vm"}
NETWORK_ROOTS = {
    "socket", "ssl", "http", "httpx", "requests", "urllib", "urllib3", "aiohttp", "websocket", "websockets",
    "ftplib", "smtplib", "telnetlib", "xmlrpc",
}
PURE_BANNED_ROOTS = {"subprocess", "random", "secrets", "uuid", "multiprocessing", "ctypes", "pickle", "marshal", "shelve"}
CLOCK_ROOTS = {"time", "datetime", "zoneinfo"}
DYNAMIC_NAMES = {"importlib", "__import__", "import_module", "exec", "eval", "compile"}
EXECUTION_ALLOWED = {"admission.py", "runner.py", "runtime.py"}
EDGE_FILES = {"admission.py", "runner.py", "runtime.py", "__main__.py"}
NO_CLOCK_FILES = {"policy.py"}
# The one module that runs another tree's code (the D-23 replay of the recorded VM build). Narrow on purpose: the
# approved functions are pinned by their complete text, each exactly once at module top level. Only nodes inside a
# function that matches its pin skip the dynamic-name, ``sys.modules`` and gateway-name rules. ``importlib`` is
# never exempt: the loader compiles the bytes it hashed and consults no import machinery.
EXEC_SOURCE_PIN = """def _exec_source(module_name: str, filename: str, source: bytes, package: str) -> Any:
    module = types.ModuleType(module_name)
    module.__file__ = filename
    module.__package__ = package
    sys.modules[module_name] = module
    code = compile(source, filename, 'exec', dont_inherit=True)
    exec(code, module.__dict__)
    return module"""
FORGET_PACKAGE_PIN = """def _forget_package(package: str) -> None:
    for name in [n for n in list(sys.modules) if n == package or n.startswith(package + '.')]:
        sys.modules.pop(name, None)"""
LOADER_FUNCTIONS: dict[str, dict[str, str]] = {
    "vm_replay.py": {"_exec_source": EXEC_SOURCE_PIN, "_forget_package": FORGET_PACKAGE_PIN},
}
LOADER_MODULE_STATEMENTS: dict[str, frozenset[str]] = {
    "vm_replay.py": frozenset({"_SOURCE_PACKAGE = 'gateway_vm'"}),
}


def pinned_text(function: ast.FunctionDef) -> str:
    """The complete text a pin is compared with: ``ast.unparse`` of the definition, its docstring left out."""

    clone = copy.deepcopy(function)
    first = clone.body[0] if clone.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        clone.body = clone.body[1:]
    return ast.unparse(clone)


def _top_level_bindings(tree: ast.Module, name: str) -> list[ast.stmt]:
    """Every top-level statement that binds ``name``: a def, a class, an assignment or an import."""

    found: list[ast.stmt] = []
    for top in tree.body:
        if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and top.name == name:
            found.append(top)
        elif isinstance(top, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in top.targets):
            found.append(top)
        elif isinstance(top, ast.AnnAssign) and isinstance(top.target, ast.Name) and top.target.id == name:
            found.append(top)
        elif isinstance(top, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) == name for a in top.names):
            found.append(top)
    return found


def _exempt_nodes(tree: ast.Module, filename: str) -> set[int]:
    """Ids of the nodes inside an approved loader function or constant of this file. Empty for every other file."""

    exempt: set[int] = set()
    for name, pin in LOADER_FUNCTIONS.get(filename, {}).items():
        bound = _top_level_bindings(tree, name)
        if len(bound) == 1 and isinstance(bound[0], ast.FunctionDef) and pinned_text(bound[0]) == pin:
            exempt.update(id(node) for node in ast.walk(bound[0]))
    for text in LOADER_MODULE_STATEMENTS.get(filename, frozenset()):
        same = [top for top in tree.body if ast.unparse(top) == text]
        if len(same) == 1:
            exempt.update(id(node) for node in ast.walk(same[0]))
    return exempt


def _duplicates(tree: ast.Module, filename: str) -> list[str]:
    return [
        f"approved loader {name} bound {len(_top_level_bindings(tree, name))} times"
        for name in LOADER_FUNCTIONS.get(filename, {})
        if len(_top_level_bindings(tree, name)) > 1
    ]


def _norm(name: str) -> str:
    return name[len("backend."):] if name.startswith("backend.") else name


PACKAGE = "shadow_india"
BEYOND_BACKEND = "__beyond_backend__"


def _roots(node: ast.AST) -> list[str]:
    """The absolute dotted module names an import node binds or reads, with a leading ``backend.`` stripped.

    A relative import is resolved against this package (``shadow_india`` sits directly under ``backend/``):
    one dot is the package itself, two dots are ``backend/`` (so ``from ..execution import x`` names
    ``execution.x``), and more dots leave ``backend/`` altogether and are reported as such.
    """

    if isinstance(node, ast.Import):
        return [_norm(alias.name) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        module = _norm(node.module or "")
        if node.level:
            if node.level > 2:
                return [f"{BEYOND_BACKEND}.{module}".rstrip(".")]
            base = PACKAGE if node.level == 1 else ""
            module = ".".join(part for part in (base, module) if part)
            if not module:  # ``from .. import execution`` names the child directly
                return [alias.name for alias in node.names]
        names = [alias.name for alias in node.names]
        if module == "backend":
            return list(names)  # ``from backend import execution`` names the package
        return [module] + [f"{module}.{name}" for name in names]
    return []


def violations(source: str, filename: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    pure = filename not in EDGE_FILES
    exempt = _exempt_nodes(tree, filename)
    found += _duplicates(tree, filename)
    for node in ast.walk(tree):
        for dotted in _roots(node):
            root = dotted.split(".")[0]
            if root == BROKER_SDK or dotted.startswith(BROKER_SDK):
                found.append(f"broker sdk import {dotted}")
            if root == BEYOND_BACKEND:
                found.append(f"relative import beyond backend {dotted}")
            if root in GATEWAY_ROOTS:
                found.append(f"gateway import {dotted}")
            if root in NETWORK_ROOTS:
                found.append(f"network import {dotted}")
            if root == "execution" and filename not in EXECUTION_ALLOWED:
                found.append(f"execution import {dotted} outside admission.py and runner.py")
            if pure and root in PURE_BANNED_ROOTS:
                found.append(f"banned import {dotted} in a pure module")
            if filename in NO_CLOCK_FILES and root in CLOCK_ROOTS:
                found.append(f"clock import {dotted} in {filename}")
            if root == "importlib":
                found.append(f"dynamic import {dotted}")
        if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES and id(node) not in exempt:
            found.append(f"dynamic or eval name {node.id}")
        if isinstance(node, ast.Attribute):
            builtins_compile = (
                node.attr == "compile" and isinstance(node.value, ast.Name) and node.value.id in {"builtins", "__builtins__"}
            )  # ``re.compile`` is ordinary; only the builtin is an eval route
            if (node.attr in DYNAMIC_NAMES and node.attr != "compile") or builtins_compile:
                found.append(f"dynamic or eval attribute {node.attr}")
            if node.attr == "modules" and isinstance(node.value, ast.Name) and node.value.id == "sys" and id(node) not in exempt:
                found.append("sys.modules")
            if filename in NO_CLOCK_FILES and node.attr in {"now", "utcnow", "today", "time", "monotonic", "sleep"}:
                found.append(f"clock call .{node.attr} in {filename}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"getattr", "setattr", "vars", "globals", "locals"}:
            args = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if node.func.id in {"vars", "globals", "locals"} or any(
                a in DYNAMIC_NAMES or a in NETWORK_ROOTS or a == "modules" for a in args
            ):
                found.append(f"reflection {node.func.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            named = {BROKER_SDK} | NETWORK_ROOTS | (set() if id(node) in exempt else GATEWAY_ROOTS)
            if node.value.split(".")[0] in named and node.value != "":
                found.append(f"module named by a string {node.value!r}")
    return found


# --- the real package ----------------------------------------------------------------------------------


def _files() -> list[Path]:
    return sorted(p for p in PKG.glob("*.py"))


def test_the_scan_covers_the_task_one_modules_and_the_edge_modules():
    names = {p.name for p in _files()}
    assert {"__init__.py", "evidence.py", "policy.py", "admission.py"} <= names


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_every_module_in_the_package_passes_the_scan(path):
    assert violations(path.read_text(), path.name) == []


def test_only_admission_the_runner_and_the_runtime_import_execution():
    importers = {
        p.name
        for p in _files()
        if any(dotted.split(".")[0] == "execution" for node in ast.walk(ast.parse(p.read_text())) for dotted in _roots(node))
    }
    assert importers <= EXECUTION_ALLOWED
    assert {"admission.py", "runtime.py"} <= importers  # the scan really sees the imports it polices


def test_the_edge_and_execution_allowlists_are_pinned_exactly():
    """Break-proof B7: widening either set to admit another module fails here, not silently."""
    assert EXECUTION_ALLOWED == {"admission.py", "runner.py", "runtime.py"}
    assert EDGE_FILES == {"admission.py", "runner.py", "runtime.py", "__main__.py"}


def test_the_runtime_builds_a_runner_only_through_assemble():
    """Break-proof B6: ``ShadowRunner(...)`` is never called in ``runtime.py``; the one constructor is ``runner.assemble``."""
    tree = ast.parse((PKG / "runtime.py").read_text())
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "ShadowRunner" not in called
    assert "assemble" in called


def test_evidence_and_policy_import_nothing_from_the_gateway_or_the_execution_spine():
    for name in ("evidence.py", "policy.py"):
        tree = ast.parse((PKG / name).read_text())
        modules = {dotted.split(".")[0] for node in ast.walk(tree) for dotted in _roots(node)}
        assert not modules & (GATEWAY_ROOTS | {"execution", "simulation", BROKER_SDK}), name


# --- plants: every one must be caught --------------------------------------------------------------------

APPROVED_EXEC_SOURCE = "\n" + EXEC_SOURCE_PIN + "\n"
APPROVED_FORGET_PACKAGE = "\n" + FORGET_PACKAGE_PIN + "\n"

PLANTS = [
    (f"import {BROKER_SDK}", "evidence.py", "broker sdk"),
    (f"from {BROKER_SDK} import BreezeConnect", "admission.py", "broker sdk"),
    (f"import {BROKER_SDK}.sub", "policy.py", "broker sdk"),
    ("import gateway_vm.orders.audit", "evidence.py", "gateway"),
    ("from gateway.vm.gateway_vm.orders.audit import AuditLog", "evidence.py", "gateway"),
    ("from gateway_vm.orders import audit", "runner.py", "gateway"),
    ("import socket", "evidence.py", "network"),
    ("import httpx", "policy.py", "network"),
    ("import requests", "runner.py", "network"),
    ("from urllib.request import urlopen", "evidence.py", "network"),
    ("from http import client", "evidence.py", "network"),
    ("import websockets", "admission.py", "network"),
    ("from execution import ledger", "evidence.py", "execution"),
    ("from execution.ledger import ExecutionLedger", "policy.py", "execution"),
    ("from backend.execution.ledger import ExecutionLedger", "evidence.py", "execution"),
    ("import backend.execution.service", "evidence.py", "execution"),
    ("from backend import execution", "policy.py", "execution"),
    ("import execution", "__main__.py", "execution"),
    ("from ..execution.ledger import ExecutionLedger", "policy.py", "execution"),
    ("from ..execution import ledger", "evidence.py", "execution"),
    ("from .. import execution", "policy.py", "execution"),
    ("from .. import execution as spine", "evidence.py", "execution"),
    ("from ..execution import *", "evidence.py", "execution"),
    ("from ..gateway_vm.orders import audit", "evidence.py", "gateway"),
    ("from .. import gateway", "policy.py", "gateway"),
    ("from ..socket import create_connection", "evidence.py", "network"),
    ("from .. import socket", "evidence.py", "network"),
    (f"from ..{BROKER_SDK} import BreezeConnect", "policy.py", "broker sdk"),
    ("from ...execution.ledger import ExecutionLedger", "policy.py", "beyond backend"),
    ("from .. import random", "evidence.py", "banned import"),
    ("import subprocess", "evidence.py", "banned import"),
    ("import random", "policy.py", "banned import"),
    ("import uuid", "evidence.py", "banned import"),
    ("import importlib", "evidence.py", "dynamic"),
    ("m = __import__('so' + 'cket')", "evidence.py", "dynamic"),
    ("import importlib\nimportlib.import_module('x')", "admission.py", "dynamic"),
    ("exec('pass')", "evidence.py", "dynamic"),
    ("eval('1')", "policy.py", "dynamic"),
    ("compile('1', 'x', 'eval')", "evidence.py", "dynamic"),
    ("import builtins\nbuiltins.compile('1', 'x', 'eval')", "evidence.py", "dynamic"),
    ("import sys\nsys.modules['socket']", "evidence.py", "sys.modules"),
    ("getattr(os, 'system')(vars())", "evidence.py", "reflection"),
    ("import os\ngetattr(os, 'modules')", "evidence.py", "reflection"),
    ("from datetime import datetime", "policy.py", "clock import"),
    ("import time", "policy.py", "clock import"),
    ("x = now.now()", "policy.py", "clock call"),
    ("x = 'socket'", "evidence.py", "named by a string"),
    (f"x = '{BROKER_SDK}'", "policy.py", "named by a string"),
    # the loader exemption is for vm_replay.py only, and only for what it needs to load a build by path
    ("import importlib.util", "evidence.py", "dynamic import"),
    ("import importlib.util", "admission.py", "dynamic import"),
    ("import importlib.util", "policy.py", "dynamic import"),
    ("import sys\nsys.modules['x']", "admission.py", "sys.modules"),
    ("x = 'gateway_vm'", "evidence.py", "named by a string"),
    ("x = 'gateway_vm'", "runner.py", "named by a string"),
    ("import importlib\nimportlib.util", "evidence.py", "dynamic or eval name importlib"),
    ("from importlib import import_module", "vm_replay.py", "dynamic import"),
    ("from importlib import machinery", "vm_replay.py", "dynamic import"),
    ("import importlib\nimportlib.import_module('x')", "vm_replay.py", "dynamic or eval attribute import_module"),
    ("exec('pass')", "vm_replay.py", "dynamic or eval name exec"),
    ("eval('1')", "vm_replay.py", "dynamic or eval name eval"),
    ("m = __import__('x')", "vm_replay.py", "dynamic or eval name __import__"),
    ("import gateway_vm.orders.limits", "vm_replay.py", "gateway import"),
    ("from gateway_vm.orders import limits", "vm_replay.py", "gateway import"),
    ("from gateway.vm.gateway_vm.orders import limits", "vm_replay.py", "gateway import"),
    ("import socket", "vm_replay.py", "network import"),
    ("import httpx", "vm_replay.py", "network import"),
    ("import requests", "vm_replay.py", "network import"),
    ("from urllib.request import urlopen", "vm_replay.py", "network import"),
    ("from brokers.breeze_gateway.client import RelayClient\nimport socket", "vm_replay.py", "network import"),
    ("x = 'socket'", "vm_replay.py", "named by a string"),
    ("from execution.ledger import ExecutionLedger", "vm_replay.py", "execution import"),
    ("from ..execution import ledger", "vm_replay.py", "execution import"),
    ("import subprocess", "vm_replay.py", "banned import"),
    ("import random", "vm_replay.py", "banned import"),
    (f"import {BROKER_SDK}", "vm_replay.py", "broker sdk"),
    ("getattr(os, 'modules')", "vm_replay.py", "reflection"),
    # importlib is no longer exempt anywhere: the loader compiles the bytes it hashed
    ("import importlib.util", "vm_replay.py", "dynamic import"),
    ("from importlib import util", "vm_replay.py", "dynamic import"),
    ("import importlib.util\nspec = importlib.util.spec_from_file_location('a', 'b')", "vm_replay.py", "dynamic import"),
    ("spec = importlib.util.spec_from_file_location('a', 'b')", "vm_replay.py", "dynamic or eval name importlib"),
    ("from importlib.machinery import SourceFileLoader", "vm_replay.py", "dynamic import"),
    # the pinned statements are exempt in vm_replay.py's named functions only
    (APPROVED_EXEC_SOURCE, "evidence.py", "dynamic or eval name exec"),
    (APPROVED_EXEC_SOURCE, "admission.py", "sys.modules"),
    (APPROVED_FORGET_PACKAGE, "policy.py", "sys.modules"),
    (APPROVED_EXEC_SOURCE.replace("_exec_source", "_other_loader"), "vm_replay.py", "dynamic or eval name exec"),
    (APPROVED_FORGET_PACKAGE.replace("_forget_package", "_other"), "vm_replay.py", "sys.modules"),
    ("def f(source, d):\n    exec(compile(source, 'x', 'exec'), d)\n", "vm_replay.py", "dynamic or eval name exec"),
    ("def f(m):\n    sys.modules['x'] = m\n", "vm_replay.py", "sys.modules"),
    ("sys.modules['x'] = m\n", "vm_replay.py", "sys.modules"),
    ("_SOURCE_PACKAGE = 'gateway_vm'", "evidence.py", "named by a string"),
    ("_OTHER = 'gateway_vm'", "vm_replay.py", "named by a string"),
    ("def f():\n    p = 'gateway_vm'\n", "vm_replay.py", "named by a string"),
]


@pytest.mark.parametrize(("source", "filename", "expected"), PLANTS, ids=[f"{i}-{p[2]}" for i, p in enumerate(PLANTS)])
def test_a_planted_violation_is_caught(source, filename, expected):
    assert any(expected in line for line in violations(source, filename)), violations(source, filename)


CLEAN = [
    ("from execution.ledger import ExecutionLedger", "admission.py"),
    ("from execution import ExecutionLedger", "runner.py"),
    ("import subprocess", "__main__.py"),
    ("from datetime import datetime", "evidence.py"),
    ("from .evidence import EvidenceStore", "policy.py"),
    ("from . import evidence", "policy.py"),
    ("from .admission import E_INTENT", "runner.py"),
    ("from ..execution.ledger import ExecutionLedger", "admission.py"),
    ("from ..execution import ExecutionLedger", "runner.py"),
    ("import os, fcntl, hashlib, json, re\nre.compile('x')", "evidence.py"),
    ("from decimal import Decimal", "policy.py"),
    # the statements vm_replay.py's two named loader functions and its directory-name constant are pinned to
    (APPROVED_EXEC_SOURCE, "vm_replay.py"),
    (APPROVED_FORGET_PACKAGE, "vm_replay.py"),
    ("_SOURCE_PACKAGE = 'gateway_vm'", "vm_replay.py"),
    ("import sys\nimport types\n" + APPROVED_EXEC_SOURCE + APPROVED_FORGET_PACKAGE + "_SOURCE_PACKAGE = 'gateway_vm'", "vm_replay.py"),
    ('def _exec_source(module_name: str, filename: str, source: bytes, package: str) -> Any:\n    """Docs may change."""\n' + "\n".join(EXEC_SOURCE_PIN.splitlines()[1:]), "vm_replay.py"),
]


@pytest.mark.parametrize(("source", "filename"), CLEAN)
def test_ordinary_imports_are_not_flagged(source, filename):
    assert violations(source, filename) == []


def test_a_relative_execution_import_planted_in_a_scratch_copy_of_a_real_module_is_flagged(tmp_path):
    for name in ("policy.py", "evidence.py"):
        scratch = tmp_path / name
        scratch.write_text((PKG / name).read_text() + "\nfrom ..execution.ledger import ExecutionLedger\n")
        assert (PKG / name).read_text() != scratch.read_text()
        assert violations((PKG / name).read_text(), name) == []
        found = violations(scratch.read_text(), name)
        assert any("execution import execution.ledger" in line for line in found), (name, found)


# --- the exemption is pinned to one loader function and its exact statements -------------------------------------

REAL_VM_REPLAY = (PKG / "vm_replay.py").read_text()

SECOND_LOADERS = {
    "exec in a new function": "\n\ndef _second_loader(source, scope):\n    exec(compile(source, 'x', 'exec'), scope)\n",
    "sys.modules in a new function": "\n\ndef _second_loader(module):\n    sys.modules['x'] = module\n",
    "importlib spec loader in a new function": (
        "\n\ndef _second_loader(path):\n    spec = importlib.util.spec_from_file_location('x', path)\n"
        "    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n"
    ),
    "importlib with its import": (
        "\n\ndef _second_loader(path):\n    import importlib.util\n"
        "    return importlib.util.spec_from_file_location('x', path)\n"
    ),
    "source file loader": "\nfrom importlib.machinery import SourceFileLoader\n",
    "the approved statements copied under another name": APPROVED_EXEC_SOURCE.replace("_exec_source", "_exec_again"),
    "the gateway name in another constant": "\n_OTHER_PACKAGE = 'gateway_vm'\n",
}


def test_the_real_vm_replay_is_clean_and_the_pins_match_its_text():
    assert violations(REAL_VM_REPLAY, "vm_replay.py") == []
    tree = ast.parse(REAL_VM_REPLAY)
    for function, pin in LOADER_FUNCTIONS["vm_replay.py"].items():
        bound = _top_level_bindings(tree, function)
        assert len(bound) == 1 and isinstance(bound[0], ast.FunctionDef), function  # defined exactly once
        assert pinned_text(bound[0]) == pin, function  # and its complete text is the pinned one
    assert _exempt_nodes(tree, "vm_replay.py")  # the pins are in use, not vacuous


@pytest.mark.parametrize("plant", sorted(SECOND_LOADERS))
def test_a_second_loader_elsewhere_in_vm_replay_is_flagged(plant):
    mutated = REAL_VM_REPLAY + SECOND_LOADERS[plant]
    assert mutated != REAL_VM_REPLAY
    assert violations(mutated, "vm_replay.py"), plant


DISALLOWED_IN_APPROVED_FUNCTION = {
    "a second exec in _exec_source": ("    exec(code, module.__dict__)\n", "    exec(code, module.__dict__)\n    exec(open('x').read(), {})\n"),
    "a second registration in _exec_source": (
        "    sys.modules[module_name] = module  # the relative imports inside the package resolve through here\n",
        "    sys.modules[module_name] = module\n    sys.modules['socket'] = module\n",
    ),
    "an exec of other scope": ("    exec(code, module.__dict__)\n", "    exec(code, {})\n"),
    "compile with another flag": (
        "    code = compile(source, filename, \"exec\", dont_inherit=True)\n", "    code = compile(source, filename, \"exec\")\n"
    ),
    "a nested loader in _exec_source": (
        "    exec(code, module.__dict__)\n", "    exec(code, module.__dict__)\n    def inner(d):\n        exec(code, d)\n"
    ),
    "a foreign module popped in _forget_package": (
        "        sys.modules.pop(name, None)\n", "        sys.modules.pop(name, None)\n    sys.modules.pop('socket', None)\n"
    ),
    "the pop aimed elsewhere": ("        sys.modules.pop(name, None)\n", "        sys.modules.pop('os', None)\n"),
    "the approved function renamed": ("def _exec_source(", "def _exec_other("),
}


@pytest.mark.parametrize("plant", sorted(DISALLOWED_IN_APPROVED_FUNCTION))
def test_a_disallowed_statement_inside_the_approved_function_is_flagged(plant):
    old, new = DISALLOWED_IN_APPROVED_FUNCTION[plant]
    assert old in REAL_VM_REPLAY, plant
    mutated = REAL_VM_REPLAY.replace(old, new, 1)
    assert mutated != REAL_VM_REPLAY
    assert violations(mutated, "vm_replay.py"), plant


DUPLICATES_AND_MOVES = {
    "a duplicate _exec_source appended": REAL_VM_REPLAY + "\n\n" + EXEC_SOURCE_PIN + "\n",
    "a duplicate _exec_source with an extra loader": (
        REAL_VM_REPLAY + "\n\ndef _exec_source(module_name, filename, source, package):\n"
        "    exec(compile(open(filename).read(), filename, 'exec'), {})\n"
    ),
    "a duplicate _forget_package appended": REAL_VM_REPLAY + "\n\n" + FORGET_PACKAGE_PIN + "\n",
    "an approved name rebound by assignment": REAL_VM_REPLAY + "\n_exec_source = lambda *a: None\n",
    "an approved name rebound by import": REAL_VM_REPLAY + "\nfrom os import system as _exec_source\n",
    "the gateway constant repeated": REAL_VM_REPLAY + "\n_SOURCE_PACKAGE = 'gateway_vm'\n",
}


@pytest.mark.parametrize("plant", sorted(DUPLICATES_AND_MOVES))
def test_a_duplicate_or_rebound_approved_loader_is_flagged(plant):
    assert violations(DUPLICATES_AND_MOVES[plant], "vm_replay.py"), plant


REPEATED_OR_MOVED = {
    "the exec statement repeated": ("    exec(code, module.__dict__)\n", "    exec(code, module.__dict__)\n    exec(code, module.__dict__)\n"),
    "the registration repeated": (
        "    sys.modules[module_name] = module  # the relative imports inside the package resolve through here\n",
        "    sys.modules[module_name] = module\n    sys.modules[module_name] = module\n",
    ),
    "the compile statement repeated": (
        "    code = compile(source, filename, \"exec\", dont_inherit=True)\n",
        "    code = compile(source, filename, \"exec\", dont_inherit=True)\n    code = compile(source, filename, \"exec\", dont_inherit=True)\n",
    ),
    "the exec statement moved into another function": (
        "    exec(code, module.__dict__)\n    return module\n",
        "    return module\n\n\ndef _elsewhere(code, module):\n    exec(code, module.__dict__)\n",
    ),
    "the registration moved into another function": (
        "    sys.modules[module_name] = module  # the relative imports inside the package resolve through here\n",
        "",
    ),
    "the cleanup loop moved into another function": (
        "        sys.modules.pop(name, None)\n", "        pass\n",
    ),
    "a signature that injects code": (
        "def _exec_source(module_name: str, filename: str, source: bytes, package: str) -> Any:",
        "def _exec_source(module_name: str, filename: str, source: bytes, package: str, code: Any = None) -> Any:",
    ),
    "a decorator added": ("def _exec_source(", "@staticmethod\ndef _exec_source("),
}


@pytest.mark.parametrize("plant", sorted(REPEATED_OR_MOVED))
def test_a_repeated_or_moved_approved_statement_is_flagged(plant):
    old, new = REPEATED_OR_MOVED[plant]
    assert old in REAL_VM_REPLAY, plant
    mutated = REAL_VM_REPLAY.replace(old, new, 1)
    if plant == "the registration moved into another function":
        mutated += "\n\ndef _elsewhere(module_name, module):\n    sys.modules[module_name] = module\n"
    if plant == "the cleanup loop moved into another function":
        mutated += "\n\ndef _elsewhere(package):\n    sys.modules.pop(package, None)\n"
    assert mutated != REAL_VM_REPLAY
    assert violations(mutated, "vm_replay.py"), plant


# --- no caller of the orders client's kill switch ---------------------------------------------------------------------


def halt_calls(source: str) -> list[int]:
    """Lines that call ``<anything>.halt(...)`` or fetch the attribute by name (``getattr(x, "halt")``).

    The shadow package never trips the VM kill switch: a broken pre-send check pauses the Mac side, and the live kill
    stays the operator's. ``release_halt`` (the portfolio latch release) is a different name and is not matched.
    """

    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "halt":
            found.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "halt"
        ):
            found.append(node.lineno)
    return found


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_no_shadow_module_calls_halt_on_an_orders_client(path):
    assert halt_calls(path.read_text()) == []


@pytest.mark.parametrize(
    "plant",
    [
        "client.halt('reason')\n",
        "self._orders.halt()\n",
        "getattr(client, 'halt')('x')\n",
        "async def f(c):\n    await c.halt('x')\n",
    ],
    ids=["attribute", "self_attribute", "getattr", "awaited"],
)
def test_a_planted_halt_call_is_caught(plant):
    real = (PKG / "runner.py").read_text()
    assert halt_calls(real) == []
    assert halt_calls(real + "\n" + plant), "the plant went unnoticed"
    assert halt_calls("portfolio.release_halt('a', 'b')\n") == []  # the latch release is not the kill switch
