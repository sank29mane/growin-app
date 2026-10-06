"""No randomness and no jitter on the Trading 212 execution path (UKT-03, D-14).

An AST scan, not a text grep: comments and docstrings may talk about jitter, but no
import, call, attribute or name may produce it.
"""

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
SCANNED = (
    [BACKEND / "trading212_mcp_server.py"]
    + sorted((BACKEND / "brokers").rglob("*.py"))
    + sorted((BACKEND / "execution").rglob("*.py"))
)
SECRETS_RANDOMNESS = {"randbelow", "randbits", "choice", "SystemRandom"}


def jitter_offences(paths) -> list[str]:
    offences: list[str] = []
    for path in paths:
        path = Path(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name
                    if root in {"random", "numpy.random"} or root.startswith("random."):
                        offences.append(f"{where} import {root}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "random" or module.startswith("random.") or module == "numpy.random":
                    offences.append(f"{where} from {module} import")
                if module == "numpy" and any(a.name == "random" for a in node.names):
                    offences.append(f"{where} from numpy import random")
                if module == "secrets" and any(a.name in SECRETS_RANDOMNESS for a in node.names):
                    offences.append(f"{where} from secrets import randomness")
            elif isinstance(node, ast.Name):
                if node.id == "random" or "jitter" in node.id.lower():
                    offences.append(f"{where} name {node.id}")
            elif isinstance(node, ast.Attribute):
                if node.attr == "random" or "jitter" in node.attr.lower():
                    offences.append(f"{where} attribute {node.attr}")
                if node.attr in SECRETS_RANDOMNESS and isinstance(node.value, ast.Name) and node.value.id == "secrets":
                    offences.append(f"{where} secrets.{node.attr}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if "jitter" in node.name.lower():
                    offences.append(f"{where} def {node.name}")
            elif isinstance(node, ast.arg):
                if "jitter" in node.arg.lower():
                    offences.append(f"{where} arg {node.arg}")
            elif isinstance(node, ast.Call):
                func = node.func
                callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if callee in {"__import__", "import_module"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value in {"random", "numpy.random"}:
                        offences.append(f"{where} dynamic import {first.value}")
    return offences


def test_the_scan_covers_the_server_the_brokers_package_and_the_execution_package():
    names = {path.name for path in SCANNED}
    assert "trading212_mcp_server.py" in names
    assert "governor.py" in names
    assert "ledger.py" in names and "service.py" in names
    assert len(SCANNED) > 10


def test_no_random_import_call_or_jitter_name_on_the_execution_path():
    assert jitter_offences(SCANNED) == []


def test_the_temporal_jitter_helper_and_the_old_budgeter_are_gone():
    source = (BACKEND / "trading212_mcp_server.py").read_text(encoding="utf-8")
    assert "_apply_temporal_jitter" not in source
    assert "get_t212_budgeter" not in source
    assert not (BACKEND / "utils" / "rate_limiter.py").exists()


def test_the_scan_catches_every_planted_form(tmp_path):
    """The guard above is only evidence if it can fail."""

    planted = tmp_path / "planted.py"
    planted.write_text(
        "import random\n"
        "from random import uniform\n"
        "import numpy.random\n"
        "from numpy import random as npr\n"
        "import secrets\n"
        "from secrets import randbelow\n"
        "async def go(self):\n"
        "    await self._apply_temporal_jitter()\n"
        "    delay = secrets.randbelow(5)\n"
        "    return random.uniform(0.5, 2.0)\n"
        "def helper(jitter=0.1):\n"
        "    return __import__('random')\n",
        encoding="utf-8",
    )
    found = " | ".join(jitter_offences([planted]))
    for expected in (
        "import random",
        "from random import",
        "import numpy.random",
        "from numpy import random",
        "from secrets import randomness",
        "attribute _apply_temporal_jitter",
        "secrets.randbelow",
        "name random",
        "arg jitter",
        "dynamic import random",
    ):
        assert expected in found, (expected, found)
