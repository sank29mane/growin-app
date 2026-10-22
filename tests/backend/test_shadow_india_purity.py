"""Phase 64-03 Task 1: purity and import rules for ``backend/shadow_india`` (P-10, T-64-12, D-05).

An AST scan, not a grep. Banned names are built from fragments so this file holds nothing GATE-02 would flag.
Rules:

1. No module imports a Breeze SDK or any gateway code (the Mac holds no broker client, and the evidence anchor
   rules are mirrored, not imported).
2. No module touches the network: sockets, HTTP clients, urllib, websockets. The VM leg goes through 63-05's
   ``RelayClient`` in ``runner.py``, which this package never reimplements.
3. ``backend/execution/*`` is imported only by ``admission.py`` and ``runner.py``.
4. Pure modules (everything but ``admission.py``, ``runner.py`` and ``__main__.py``) have no subprocess, no
   randomness, no dynamic import or eval, no ``sys.modules``; ``policy.py`` also has no clock at all.

Plants below are regression cases for the scanner itself.
"""

from __future__ import annotations

import ast
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
EXECUTION_ALLOWED = {"admission.py", "runner.py"}
EDGE_FILES = {"admission.py", "runner.py", "__main__.py"}
NO_CLOCK_FILES = {"policy.py"}


def _norm(name: str) -> str:
    return name[len("backend."):] if name.startswith("backend.") else name


def _roots(node: ast.AST) -> list[str]:
    """The dotted module names an import node binds or reads, with a leading ``backend.`` stripped."""

    if isinstance(node, ast.Import):
        return [_norm(alias.name) for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return []  # a relative import stays inside the package
        module = _norm(node.module or "")
        names = [alias.name for alias in node.names]
        if module == "backend":
            return list(names)  # ``from backend import execution`` names the package
        return [module] + [f"{module}.{name}" for name in names]
    return []


def violations(source: str, filename: str) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    pure = filename not in EDGE_FILES
    for node in ast.walk(tree):
        for dotted in _roots(node):
            root = dotted.split(".")[0]
            if root == BROKER_SDK or dotted.startswith(BROKER_SDK):
                found.append(f"broker sdk import {dotted}")
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
        if isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES:
            found.append(f"dynamic or eval name {node.id}")
        if isinstance(node, ast.Attribute):
            builtins_compile = (
                node.attr == "compile" and isinstance(node.value, ast.Name) and node.value.id in {"builtins", "__builtins__"}
            )  # ``re.compile`` is ordinary; only the builtin is an eval route
            if (node.attr in DYNAMIC_NAMES and node.attr != "compile") or builtins_compile:
                found.append(f"dynamic or eval attribute {node.attr}")
            if node.attr == "modules" and isinstance(node.value, ast.Name) and node.value.id == "sys":
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
            if node.value.split(".")[0] in ({BROKER_SDK} | GATEWAY_ROOTS | NETWORK_ROOTS) and node.value != "":
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


def test_only_admission_and_the_runner_import_execution():
    importers = {
        p.name
        for p in _files()
        if any(dotted.split(".")[0] == "execution" for node in ast.walk(ast.parse(p.read_text())) for dotted in _roots(node))
    }
    assert importers <= EXECUTION_ALLOWED
    assert "admission.py" in importers  # the scan really sees the imports it polices


def test_evidence_and_policy_import_nothing_from_the_gateway_or_the_execution_spine():
    for name in ("evidence.py", "policy.py"):
        tree = ast.parse((PKG / name).read_text())
        modules = {dotted.split(".")[0] for node in ast.walk(tree) for dotted in _roots(node)}
        assert not modules & (GATEWAY_ROOTS | {"execution", "simulation", BROKER_SDK}), name


# --- plants: every one must be caught --------------------------------------------------------------------

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
    ("import os, fcntl, hashlib, json, re\nre.compile('x')", "evidence.py"),
    ("from decimal import Decimal", "policy.py"),
]


@pytest.mark.parametrize(("source", "filename"), CLEAN)
def test_ordinary_imports_are_not_flagged(source, filename):
    assert violations(source, filename) == []
