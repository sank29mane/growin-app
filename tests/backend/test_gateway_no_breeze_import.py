"""GATE-02: nothing in the repo may import or list the vendor Breeze SDK.

This is absolute. There is no allowlist and no exemption, including for this
file, so the SDK's names are assembled from fragments at runtime and the two
regex literals below are written so they do not match themselves.

Known limit: a name assembled at runtime and passed to importlib is invisible
to an AST scan. The fresh-interpreter sys.modules check in Plan 61-15 covers
that case.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Built from fragments so this file holds no constant its own scan would flag.
SDK_IMPORT = "_".join(("breeze", "connect"))
SDK_DIST = "-".join(("breeze", "connect"))

# Neither literal matches itself: the first starts with "^", the second has "["
# right after the first word.
STRING_IMPORT_RE = re.compile(r"^breeze[-_.]?connect(\.|$)", re.IGNORECASE)
TEXT_RE = re.compile(r"breeze[-_.]?connect", re.IGNORECASE)

MANIFEST_PATTERNS = (
    "pyproject.toml",
    "requirements*.txt",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "setup.py",
    "setup.cfg",
)


@dataclass(frozen=True)
class Offence:
    path: str
    line: int
    kind: str  # import | from_import | string_import | manifest | unparsable_text


def _listed_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise AssertionError("the GATE-02 scan needs git; it fails rather than skips") from exc
    return [line for line in proc.stdout.splitlines() if line]


def _is_manifest(rel: str) -> bool:
    name = Path(rel).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in MANIFEST_PATTERNS)


def _norm_module(name: str) -> str:
    return name.split(".")[0].lower().replace("-", "_")


def _text_offences(rel: str, text: str, kind: str) -> list[Offence]:
    return [
        Offence(rel, number, kind)
        for number, line in enumerate(text.splitlines(), start=1)
        if TEXT_RE.search(line)
    ]


def _python_offences(rel: str, raw: bytes) -> list[Offence]:
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError):
        # Cannot parse (for example syntax newer than this interpreter):
        # fall back to a text search, an offence only if it matches.
        return _text_offences(rel, raw.decode("utf-8", errors="replace"), "unparsable_text")
    found: list[Offence] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _norm_module(alias.name) == SDK_IMPORT:
                    found.append(Offence(rel, node.lineno, "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and _norm_module(node.module) == SDK_IMPORT:
                found.append(Offence(rel, node.lineno, "from_import"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if STRING_IMPORT_RE.match(node.value):
                found.append(Offence(rel, node.lineno, "string_import"))
    return found


def find_offences(root: Path) -> list[Offence]:
    offences: list[Offence] = []
    for rel in _listed_files(root):
        path = root / rel
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if rel.endswith(".py"):
            offences.extend(_python_offences(rel, raw))
        if _is_manifest(rel):
            offences.extend(
                _text_offences(rel, raw.decode("utf-8", errors="replace"), "manifest")
            )
    return offences


def _format(offences: list[Offence]) -> str:
    return "\n".join(f"{o.path}:{o.line}: {o.kind}" for o in offences)


# ----------------------------------------------------------------- the gate


def test_repo_never_names_the_vendor_sdk():
    offences = find_offences(ROOT)
    assert offences == [], "GATE-02 offences:\n" + _format(offences)


# ------------------------------------------------- the gate can fail (planted)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_scan_detects_every_planted_offence(tmp_path):
    _git_init(tmp_path)
    files = {
        "a.py": f"import {SDK_IMPORT}\n",
        "b.py": f"from {SDK_IMPORT} import X\n",
        "c.py": f'import importlib\nimportlib.import_module("{SDK_DIST}")\n',
        "requirements.txt": f"{SDK_DIST}==1.0.69\n",
        "pyproject.toml": f'[project]\ndependencies = ["{SDK_DIST}>=1.0"]\n',
        "broken.py": f"def broken(:\n    # mentions {SDK_IMPORT}\n",
        "clean.py": "import os\nprint(os.name)\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)

    found = {(o.path, o.kind) for o in find_offences(tmp_path)}
    assert ("a.py", "import") in found
    assert ("b.py", "from_import") in found
    assert ("c.py", "string_import") in found
    assert ("requirements.txt", "manifest") in found
    assert ("pyproject.toml", "manifest") in found
    assert ("broken.py", "unparsable_text") in found
    assert not any(path == "clean.py" for path, _kind in found)


def test_scan_reports_line_numbers(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "late.py").write_text(f"x = 1\ny = 2\nimport {SDK_IMPORT}\n")
    [offence] = find_offences(tmp_path)
    assert (offence.path, offence.line, offence.kind) == ("late.py", 3, "import")


def test_scan_ignores_gitignored_files(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n")
    (tmp_path / "ignored.py").write_text(f"import {SDK_IMPORT}\n")
    assert find_offences(tmp_path) == []


def test_scan_fails_when_git_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(AssertionError):
        find_offences(tmp_path)
