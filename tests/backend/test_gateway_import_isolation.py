"""GATE-02 at import time: neither side of the gateway ever loads the vendor Breeze SDK.

The AST scan in test_gateway_no_breeze_import.py cannot see a name assembled at runtime and
handed to importlib. This file closes that gap: it imports every module of both packages in a
fresh interpreter and counts the SDK's entries in sys.modules. A stub package of the same name
proves the count can go above zero.

The SDK's import name is built from fragments here, as in the AST scan, because that scan
rejects the name as a string constant in any .py file.
"""

from __future__ import annotations

import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
GATEWAY_VM = ROOT / "gateway" / "vm"

# Built from fragments: the 61-01 scan rejects this name as one string literal.
SDK_NAME = "_".join(("breeze", "connect"))

# The code a fresh interpreter runs. The SDK name arrives as argv[1], never as a literal here.
_CHILD = """
import importlib, json, sys
extra, names, sdk = json.loads(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]
sys.path[:0] = extra
for name in names:
    importlib.import_module(name)
hits = [m for m in sys.modules if m.split(".")[0].lower().replace("-", "_") == sdk]
print(len(hits))
"""

# A bare environment: nothing from the caller's shell, no user site, no bytecode written.
_ENV = {"PATH": "", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def count_sdk_modules(extra_paths: list[Path], imports: list[str]) -> int:
    """Import every name in a fresh interpreter and count the SDK's sys.modules entries.

    A failed import is an error, never a zero: a module that cannot load proves nothing.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            json.dumps([str(path) for path in extra_paths]),
            json.dumps(imports),
            SDK_NAME,
        ],
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"fresh interpreter failed ({proc.returncode}): {proc.stderr[-600:]}")
    return int(proc.stdout.strip().splitlines()[-1])


def walk_package(root: Path, package: str) -> list[str]:
    """Every module and subpackage under `package`, found on disk without importing any."""
    found = [package]
    directory = root.joinpath(*package.split("."))
    for info in pkgutil.iter_modules([str(directory)]):
        name = f"{package}.{info.name}"
        if info.name == "__main__":
            continue  # running it would start the program
        if info.ispkg:
            found.extend(walk_package(root, name))
        else:
            found.append(name)
    return sorted(set(found))


def gateway_modules() -> tuple[list[str], list[str]]:
    return (
        walk_package(BACKEND, "brokers.breeze_gateway"),
        walk_package(GATEWAY_VM, "gateway_vm"),
    )


# ------------------------------------------------------------------ I2


def test_the_walk_cannot_come_back_empty():
    mac, vm = gateway_modules()
    names = set(mac) | set(vm)
    assert {
        "gateway_vm.main",
        "gateway_vm.orders.routes",
        "gateway_vm.orders.verify",
        "brokers.breeze_gateway.cli",
        "brokers.breeze_gateway.orders",
    } <= names
    assert len(names) > 25  # a walk that found only the packages themselves cannot pass


# ------------------------------------------------------------------ I1


def test_no_gateway_module_loads_the_vendor_sdk_in_a_fresh_interpreter():
    mac, vm = gateway_modules()
    assert count_sdk_modules([BACKEND, GATEWAY_VM], mac + vm) == 0


def test_each_side_alone_stays_clean():
    mac, vm = gateway_modules()
    assert count_sdk_modules([BACKEND], mac) == 0
    assert count_sdk_modules([GATEWAY_VM], vm) == 0


# ------------------------------------------------------------------ I3


def make_stub(root: Path) -> None:
    """A package named like the SDK, plus a module that imports it the way a hidden import would."""
    stub = root / SDK_NAME
    stub.mkdir()
    (stub / "__init__.py").write_text("", encoding="utf-8")
    (root / "uses_vendor_sdk.py").write_text(
        "import importlib\n"
        f"importlib.import_module({SDK_NAME!r})\n",
        encoding="utf-8",
    )


def test_negative_control_a_planted_stub_is_counted(tmp_path):
    make_stub(tmp_path)
    assert count_sdk_modules([tmp_path], ["uses_vendor_sdk"]) >= 1


def test_negative_control_the_stub_is_inert_until_something_imports_it(tmp_path):
    make_stub(tmp_path)
    mac, vm = gateway_modules()
    # On the path but never imported: still zero, so the count above comes from the import.
    assert count_sdk_modules([tmp_path, BACKEND, GATEWAY_VM], mac + vm) == 0


def test_a_module_that_fails_to_import_is_an_error_not_a_zero(tmp_path):
    with pytest.raises(RuntimeError):
        count_sdk_modules([tmp_path], ["no_such_module_for_the_isolation_check"])
