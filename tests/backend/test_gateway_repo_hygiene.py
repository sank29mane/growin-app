"""Repo hygiene tripwires for the Phase 61 gateway trees (GATE-03).

Covers key-file ignores plus an AST scan of gateway/ and backend/brokers/ for
dotenv use, stray environment reads and credential-shaped string literals.
The scan lists files with `git ls-files -co --exclude-standard`, so it passes
while those trees are empty and starts biting as soon as files land.

This file deliberately does not scan tests: fake values live there.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCAN_PREFIXES = ("gateway/", "backend/brokers/")
ALLOWED_ENV_NAMES = frozenset(
    {"CREDENTIALS_DIRECTORY", "STATE_DIRECTORY", "HOME", "XDG_CONFIG_HOME", "TMPDIR"}
)
B64_SHAPE = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
PLACEHOLDER = re.compile(r"^<[A-Z0-9_]+>$")


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:  # fail, never skip
        raise AssertionError("git is required for the hygiene tests") from exc
    if proc.returncode not in (0, 1):
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout if proc.returncode == 0 else ""


def _listed_files() -> list[str]:
    out = _git("ls-files", "-co", "--exclude-standard")
    return [line for line in out.splitlines() if line]


def _is_ignored(path: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=ROOT, capture_output=True
        )
    except FileNotFoundError as exc:
        raise AssertionError("git is required for the hygiene tests") from exc
    return proc.returncode == 0


# ------------------------------------------------------------ source scan


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _is_getenv(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "getenv":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "getenv"


def _env_arg_ok(arg: ast.AST | None) -> bool:
    return (
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and arg.value in ALLOWED_ENV_NAMES
    )


def _base64_credential(value: str) -> bool:
    if not B64_SHAPE.match(value):
        return False
    try:
        text = base64.b64decode(value, validate=True).decode("ascii")
    except Exception:
        return False
    left, sep, right = text.partition(":")
    return bool(sep and left and right)


def scan_source(source: str) -> list[tuple[int, str]]:
    """Return (line, kind) offences for one Python source text."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "dotenv":
                    found.append((node.lineno, "dotenv import"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "dotenv":
                found.append((node.lineno, "dotenv import"))
        elif isinstance(node, ast.Call):
            func = node.func
            first = node.args[0] if node.args else None
            if _is_getenv(func):
                if not _env_arg_ok(first):
                    found.append((node.lineno, "env read"))
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in ("get", "setdefault")
                and _is_environ(func.value)
            ):
                if not _env_arg_ok(first):
                    found.append((node.lineno, "env read"))
        elif isinstance(node, ast.Subscript):
            if _is_environ(node.value) and not _env_arg_ok(node.slice):
                found.append((node.lineno, "env read"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if HEX64.match(node.value):
                found.append((node.lineno, "64-hex literal"))
            elif _base64_credential(node.value):
                found.append((node.lineno, "session-token-shaped literal"))
    return found


# ------------------------------------------------------------- key files


@pytest.mark.parametrize(
    "path", ["x.pem", "gateway/vm/a.key", "backend/brokers/b.p12", "c.pfx"]
)
def test_key_file_extensions_are_ignored(path):
    assert _is_ignored(path), f"{path} must be git-ignored"


def test_no_key_file_is_tracked():
    tracked = _git("ls-files").splitlines()
    bad = [p for p in tracked if p.endswith((".pem", ".key", ".p12", ".pfx"))]
    assert bad == []


# -------------------------------------------------- gateway tree tripwires


def test_gateway_trees_have_no_credential_patterns():
    offences = []
    for rel in _listed_files():
        if not rel.startswith(SCAN_PREFIXES) or not rel.endswith(".py"):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            found = scan_source(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            offences.append(f"{rel}:{exc.lineno}: unparsable")
            continue
        offences.extend(f"{rel}:{line}: {kind}" for line, kind in found)
    assert offences == [], "\n".join(offences)


def test_example_json_values_are_placeholders():
    bad = []

    def walk(rel: str, node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(rel, value)
        elif isinstance(node, list):
            for value in node:
                walk(rel, value)
        elif isinstance(node, str):
            ok = (
                PLACEHOLDER.match(node)
                or node.startswith("https://")
                or node.startswith("breeze-api-")
                or node.startswith("~/.config/growin/")
            )
            if not ok:
                bad.append(f"{rel}: non-placeholder string value")

    for rel in _listed_files():
        if rel.startswith("gateway/") and rel.endswith(".example.json"):
            path = ROOT / rel
            if path.is_file():
                walk(rel, json.loads(path.read_text(encoding="utf-8")))
    assert bad == []


# ------------------------------------------- the tripwire itself can fire


def _planted() -> dict[str, str]:
    hex64 = hashlib.sha256(os.urandom(16)).hexdigest()
    b64 = base64.b64encode(b"FAKEUSER:12345678").decode()
    return {
        "dotenv import": "import dotenv\n",
        "dotenv import from": "from dotenv import load_dotenv\n",
        "getenv": 'import os\nx = os.getenv("AWS_SECRET")\n',
        "environ get": 'import os\nx = os.environ.get("AWS_SECRET")\n',
        "environ index": 'import os\nx = os.environ["AWS_SECRET"]\n',
        "environ setdefault": 'import os\nos.environ.setdefault("AWS_SECRET", "x")\n',
        "environ dynamic": "import os\nname = 'HOME'\nx = os.getenv(name)\n",
        "hex literal": f'KEY = "{hex64}"\n',
        "token literal": f'TOKEN = "{b64}"\n',
    }


@pytest.mark.parametrize("name", list(_planted()))
def test_scanner_flags_planted_offence(name):
    assert scan_source(_planted()[name]), f"scanner missed: {name}"


def test_scanner_accepts_allowlisted_env_and_plain_strings():
    source = (
        "import os\n"
        'a = os.environ.get("CREDENTIALS_DIRECTORY")\n'
        'b = os.environ["HOME"]\n'
        'c = os.getenv("XDG_CONFIG_HOME", "x")\n'
        'd = "https://example.invalid/v1/quotes"\n'
        'e = "growin.smoke.breeze.v1"\n'
    )
    assert scan_source(source) == []
