"""Forward-covering static guards for the whole backend/pilot_data package.

Every later plan re-runs this file. It walks every module with ast, so modules added
later are covered without edits here.
"""

import ast
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import pilot_data
from pilot_data.core import (
    HINDSIGHT_CAVEAT,
    SURVIVORSHIP_CAVEAT,
    CaveatedResult,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "backend" / "pilot_data"
TESTS_DIR = REPO_ROOT / "tests" / "backend"
# Built at runtime: a literal here would trip Phase 61's repo-wide scan for the SDK module name.
FORBIDDEN_SDK_MODULE = "_".join(("breeze", "connect"))
SDK_NAME_PATTERN = re.compile("^" + "breeze" + "[-_.]?" + "connect", re.IGNORECASE)
MUTATING_SQL = [
    re.compile(r"\bUPDATE\b.*\bSET\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE),
    re.compile(r"\bON\s+CONFLICT\b.*\bDO\s+UPDATE\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
]


def package_files() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def pilot_test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_pilot_data_*.py")) + [TESTS_DIR / "pilot_data_testkit.py"]


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def string_constants(tree: ast.Module, *, include_docstrings: bool) -> list[str]:
    skip = set() if include_docstrings else docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def test_package_has_files_to_scan():
    assert package_files(), "the guard walks backend/pilot_data and found nothing"


def test_no_module_imports_the_sdk_or_names_it_as_a_string():
    for path in package_files():
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(FORBIDDEN_SDK_MODULE), f"{path} imports the SDK"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(FORBIDDEN_SDK_MODULE), f"{path} imports the SDK"
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name in {"import_module", "__import__"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        assert not first.value.startswith(FORBIDDEN_SDK_MODULE), f"{path} imports the SDK"


def test_no_file_in_package_or_tests_holds_the_sdk_name_as_a_string_constant():
    for path in package_files() + pilot_test_files():
        for value in string_constants(parse(path), include_docstrings=True):
            assert not SDK_NAME_PATTERN.match(value.strip()), f"{path} holds the SDK module name as one string"
            assert FORBIDDEN_SDK_MODULE not in value, f"{path} holds the SDK module name inside a string"


def test_no_non_docstring_string_addresses_a_broker_host():
    host = "icici" + "direct.com"
    for path in package_files():
        for value in string_constants(parse(path), include_docstrings=False):
            assert host not in value.lower(), f"{path} addresses a broker host"


def test_no_module_reads_environment_variables():
    for path in package_files():
        tree = parse(path)
        os_aliases = {"os"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os":
                        os_aliases.add(alias.asname or "os")
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                for alias in node.names:
                    assert alias.name not in {"environ", "getenv", "environb", "getenvb"}, f"{path}: {alias.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb", "getenvb"}:
                value = node.value
                assert not (isinstance(value, ast.Name) and value.id in os_aliases), f"{path}: os.{node.attr}"


def test_no_string_literal_holds_row_mutating_sql():
    for path in package_files():
        for value in string_constants(parse(path), include_docstrings=True):
            for pattern in MUTATING_SQL:
                assert not pattern.search(value), f"{path}: mutating SQL in string {value[:60]!r}"


def package_modules():
    for info in pkgutil.walk_packages(pilot_data.__path__, prefix="pilot_data."):
        yield importlib.import_module(info.name)


RESULT_SUFFIXES = ("Result", "Report", "Manifest", "Series", "Check")


def test_every_result_type_subclasses_caveated_result():
    checked = 0
    for module in package_modules():
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or cls is CaveatedResult:
                continue
            if issubclass(cls, BaseModel) and name.endswith(RESULT_SUFFIXES):
                checked += 1
                assert issubclass(cls, CaveatedResult), f"{module.__name__}.{name} must subclass CaveatedResult"
    assert checked >= 1


class _Probe(CaveatedResult):
    note: str = "x"


def test_building_a_result_without_the_standard_caveats_fails():
    with pytest.raises(ValidationError):
        _Probe(workspace="india", caveats=())
    with pytest.raises(ValidationError):
        _Probe(workspace="india", caveats=(SURVIVORSHIP_CAVEAT,))
    with pytest.raises(ValidationError):
        _Probe(workspace="india", caveats=(HINDSIGHT_CAVEAT,))
    with pytest.raises(ValidationError):
        _Probe(caveats=(SURVIVORSHIP_CAVEAT, HINDSIGHT_CAVEAT))
    assert _Probe(workspace="india", caveats=(SURVIVORSHIP_CAVEAT, HINDSIGHT_CAVEAT)).note == "x"
