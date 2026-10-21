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


# ---------------------------------------------------------------- one writer for coverage rows
#
# `breeze_responses` rows are what BreezeBarsView.covers() reads: a row says "the store knows every session in
# this range". Only `write_breeze_coverage` may write one (it refuses anything not judged complete), through the
# one low-level insert `_insert_coverage_row`. The scan is simple on purpose: it reads the AST for the two ways
# an honest author would add a second writer (an insert call that names the table, or a call to the low-level
# insert). It is not a defence against a hostile author, who has easier ways to the same table.

COVERAGE_TABLE = "breeze_responses"
LOW_LEVEL_COVERAGE_INSERT = "_insert_coverage_row"
INSERT_CALLS = {"append_rows", "executemany", "execute", "insert", "insert_rows", "upsert"}
INSERT_WORDS = re.compile(r"\b(INSERT|REPLACE|UPSERT|MERGE)\b", re.IGNORECASE)
ALLOWED_TABLE_INSERT_SITES = {("breeze_bars.py", LOW_LEVEL_COVERAGE_INSERT)}
ALLOWED_LOW_LEVEL_CALL_SITES = {("breeze_bars.py", "write_breeze_coverage")}


def call_name(node: ast.Call) -> str:
    return getattr(node.func, "attr", getattr(node.func, "id", ""))


def enclosing_function_names(tree: ast.Module) -> dict[int, str]:
    """Node id -> innermost enclosing function name (outer functions are visited first, inner ones overwrite)."""
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[id(child)] = fn.name
    return owner


def coverage_write_sites(filename: str, source: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(sites that insert into the coverage table, sites that call the low-level coverage insert)."""
    tree = ast.parse(source, filename=filename)
    owner = enclosing_function_names(tree)
    docstrings = docstring_nodes(tree)
    inserts: list[tuple[str, str]] = []
    low_level: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            where = (filename, owner.get(id(node), "<module>"))
            if call_name(node) == LOW_LEVEL_COVERAGE_INSERT:
                low_level.append(where)
            strings = [
                arg.value for arg in (*node.args, *(kw.value for kw in node.keywords))
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if call_name(node) in INSERT_CALLS and any(
                value == COVERAGE_TABLE or (COVERAGE_TABLE in value and INSERT_WORDS.search(value))
                for value in strings
            ):
                inserts.append(where)
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
            and COVERAGE_TABLE in node.value and INSERT_WORDS.search(node.value)
        ):
            inserts.append((filename, owner.get(id(node), "<module>")))  # raw INSERT text, wherever it is used
    return inserts, low_level


def test_only_write_breeze_coverage_writes_coverage_rows():
    inserts: list[tuple[str, str]] = []
    low_level: list[tuple[str, str]] = []
    for path in package_files():
        found_inserts, found_low_level = coverage_write_sites(path.name, path.read_text(encoding="utf-8"))
        inserts += found_inserts
        low_level += found_low_level
    assert set(inserts) == ALLOWED_TABLE_INSERT_SITES, f"another writer inserts into {COVERAGE_TABLE}: {inserts}"
    assert len(inserts) == 1, inserts  # not zero: the scan must see the one real writer, or it checks nothing
    assert set(low_level) == ALLOWED_LOW_LEVEL_CALL_SITES, f"another caller of {LOW_LEVEL_COVERAGE_INSERT}: {low_level}"
    assert len(low_level) == 1, low_level


@pytest.mark.parametrize(
    "source",
    [
        'def sneak(store, row):\n    store.append_rows("breeze_responses", [row], check="x")\n',
        'def sneak(store, row):\n    store.execute("INSERT INTO breeze_responses VALUES (?)", [row])\n',
        'SQL = "INSERT INTO breeze_responses(source_sha256) VALUES (?)"\n',
    ],
    ids=["append_rows", "execute_insert", "raw_insert_text"],
)
def test_the_coverage_scan_flags_a_second_writer(source):
    inserts, _ = coverage_write_sites("other.py", source)
    assert inserts and not set(inserts) <= ALLOWED_TABLE_INSERT_SITES


def test_the_coverage_scan_flags_a_second_caller_of_the_low_level_insert():
    _, low_level = coverage_write_sites("other.py", "def sneak(store, row):\n    _insert_coverage_row(store, row)\n")
    assert low_level == [("other.py", "sneak")]


def test_the_coverage_scan_leaves_reads_and_the_table_definition_alone():
    source = (
        'def read(store):\n    return store.query("SELECT source_sha256 FROM breeze_responses")\n'
        'def define(store):\n    store.ensure_table("breeze_responses", DDL, key_columns=("source_sha256",))\n'
    )
    assert coverage_write_sites("other.py", source) == ([], [])
