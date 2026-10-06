"""AC-8 and AC-9: static guards for the model role registry.

AC-8: an AST scan of the LLM runtime surface finds no name-based provider or
model guessing. AC-9: no hard-coded model defaults and no legacy aliases.

Each detector has planted-source tests proving it fires, so a green scan means
the detector works and the tree is clean.
"""

import ast
import re
from pathlib import Path
from typing import Iterable, List, Optional, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
GROWIN = REPO_ROOT / "Growin"

# --- AC-8: name-based guessing ---------------------------------------------------

NAME_HINTS = ("model", "provider")
STRING_PREDICATES = {"startswith", "endswith", "find", "rfind", "index", "count", "__contains__"}
REGEX_FUNCS = {"match", "search", "fullmatch"}

# The LLM runtime surface (repo-relative globs). Not scanned, by decision:
# utils/data_frayer.py (market-data providers), backtest_lab/, models/clara/,
# mlx_engine.py:142 and mlx_vlm_engine.py:44 (deferred).
SURFACE_GLOBS = (
    "backend/agents/*.py",
    "backend/model_registry/*.py",
    "backend/routes/chat_routes.py",
    "backend/routes/agent_routes.py",
    "backend/routes/ai_routes.py",
    "backend/app_context.py",
    "backend/server.py",
    "backend/lm_studio_client.py",
    "backend/mlx_langchain.py",
    "backend/forecaster.py",
    "backend/forecast_bridge.py",
    "backend/status_manager.py",
    "backend/utils/worker_client.py",
    "backend/utils/worker_service.py",
    "backend/utils/rstitch_engine.py",
)

# No site is allowlisted: the provider kind dispatch in model_registry/provider.py
# is a dict lookup (``_CHAT_BUILDERS.get(resolved.kind)``), not a string compare.
ALLOWED_SITES: Set[tuple] = set()


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _holds_str_const(node: ast.AST) -> bool:
    if _is_str_const(node):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_str_const(elt) for elt in node.elts)
    return False


def _hinted(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in NAME_HINTS)


def _identifiers(node: ast.AST) -> List[str]:
    """Every name, attribute and string key an expression touches."""

    found: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.append(child.id)
        elif isinstance(child, ast.Attribute):
            found.append(child.attr)
        elif isinstance(child, ast.Subscript) and _is_str_const(child.slice):
            found.append(child.slice.value)
        elif (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "get"
            and child.args
            and _is_str_const(child.args[0])
        ):
            found.append(child.args[0].value)
    return found


def _expr_hinted(node: ast.AST) -> bool:
    return any(_hinted(name) for name in _identifiers(node))


class _Scanner(ast.NodeVisitor):
    def __init__(self, relpath: str) -> None:
        self.relpath = relpath
        self.findings: List[str] = []
        self._const_names: List[Set[str]] = [set()]

    # Loop and comprehension targets bound to a list of string constants act as
    # string constants inside the loop: ``any(k in model.lower() for k in ["a"])``.
    def _bound_consts(self, generators: Iterable[ast.comprehension]) -> Set[str]:
        names: Set[str] = set()
        for gen in generators:
            if _holds_str_const(gen.iter) and isinstance(gen.target, ast.Name):
                names.add(gen.target.id)
        return names

    def _visit_comprehension(self, node: ast.AST) -> None:
        self._const_names.append(self._const_names[-1] | self._bound_consts(node.generators))
        self.generic_visit(node)
        self._const_names.pop()

    visit_GeneratorExp = _visit_comprehension
    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension

    def visit_For(self, node: ast.For) -> None:
        extra: Set[str] = set()
        if _holds_str_const(node.iter) and isinstance(node.target, ast.Name):
            extra.add(node.target.id)
        self._const_names.append(self._const_names[-1] | extra)
        self.generic_visit(node)
        self._const_names.pop()

    def _is_const_like(self, node: ast.AST) -> bool:
        if _holds_str_const(node):
            return True
        return isinstance(node, ast.Name) and node.id in self._const_names[-1]

    def _report(self, node: ast.AST, what: str) -> None:
        site = (self.relpath, node.lineno)
        if site not in ALLOWED_SITES:
            self.findings.append(f"{self.relpath}:{node.lineno}: {what}")

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        const_operands = [op for op in operands if self._is_const_like(op)]
        others = [op for op in operands if not self._is_const_like(op)]
        if const_operands and any(_expr_hinted(op) for op in others):
            self._report(node, "compare of a string constant with a model or provider expression")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in STRING_PREDICATES and any(self._is_const_like(a) for a in node.args):
                if _expr_hinted(func.value):
                    self._report(node, f".{func.attr}() with a string constant on a model or provider expression")
            if func.attr in REGEX_FUNCS and node.args:
                has_const = any(_is_str_const(a) for a in node.args)
                if has_const and any(_expr_hinted(a) for a in node.args if not _is_str_const(a)):
                    self._report(node, "regex match of a string constant against a model or provider expression")
        self.generic_visit(node)


def scan_source(source: str, relpath: str = "<planted>") -> List[str]:
    scanner = _Scanner(relpath)
    scanner.visit(ast.parse(source))
    return scanner.findings


def surface_files() -> List[Path]:
    files: List[Path] = []
    for pattern in SURFACE_GLOBS:
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    return files


def test_surface_is_found():
    names = {p.name for p in surface_files()}
    for expected in (
        "decision_agent.py",
        "risk_agent.py",
        "llm_factory.py",
        "provider.py",
        "chat_routes.py",
        "worker_client.py",
        "lm_studio_client.py",
    ):
        assert expected in names


def test_no_name_based_model_or_provider_guessing_in_the_runtime_surface():
    findings: List[str] = []
    for path in surface_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        findings.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    assert findings == [], "name-based guessing remains:\n" + "\n".join(findings)


@pytest.mark.parametrize(
    "planted",
    [
        'x = "gemma-4" in self.model_name.lower()',
        'ok = provider == "lmstudio"',
        'is_hf = "/" in model_lower',
        'ok = info.provider != "ollama"',
        'small = any(k in (self.model_name or "").lower() for k in ["nano", "tiny"])',
        'v = self.model_name.startswith("claude")',
        'v = provider.endswith(("-mlx", "-gguf"))',
        'ok = "gpt" in info["provider"]',
        'ok = "oss" not in cfg.get("model_id")',
        'import re\nok = re.match("^lm", model_name)',
        'for key in ("granite", "gemma"):\n    if key in model_name:\n        pass',
    ],
)
def test_planted_name_guessing_is_caught(planted):
    assert scan_source(planted), planted


@pytest.mark.parametrize(
    "clean",
    [
        'ok = role == "decision"',
        'ok = msg.get("role") == "user"',
        "ok = resolved.kind is KIND",
        "ok = provider is None",
        "ok = model in KNOWN",
        'flag = "embed" in text',
        "kind_builder = BUILDERS.get(resolved.kind)",
    ],
)
def test_ordinary_code_is_not_flagged(clean):
    assert scan_source(clean) == [], clean


# --- AC-9 ---------------------------------------------------------------------------

DEFAULT_FORBIDDEN_NAMES = {
    "model",
    "model_name",
    "model_id",
    "model_path",
    "routing_model",
    "coordinator_model",
}

LEGACY_LITERALS = (
    "lmstudio-auto",
    "native-mlx",
    "granite-tiny",
    "nemotron-",
    "gpt-oss",
    "gpt-4o",
    "granite-4.0",
    "mlx-community/",
    "lmstudio-community/",
    "ibm-granite/",
    "claude-3",
    "gemini-3",
)


def _backend_python_files() -> List[Path]:
    return sorted(
        p
        for p in BACKEND.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    )


def _swift_files() -> List[Path]:
    return sorted(GROWIN.rglob("*.swift"))


def scan_model_defaults(source: str, relpath: str = "<planted>") -> List[str]:
    """Parameters and class fields named like a model with a non-None str default."""

    findings: List[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            for arg, default in zip(positional[len(positional) - len(args.defaults):], args.defaults):
                if arg.arg in DEFAULT_FORBIDDEN_NAMES and _is_str_const(default):
                    findings.append(f"{relpath}:{node.lineno}: parameter {arg.arg} has a str default")
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                if default is not None and arg.arg in DEFAULT_FORBIDDEN_NAMES and _is_str_const(default):
                    findings.append(f"{relpath}:{node.lineno}: parameter {arg.arg} has a str default")
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id in DEFAULT_FORBIDDEN_NAMES:
                value = node.value
                if value is not None and _is_str_const(value):
                    findings.append(f"{relpath}:{node.lineno}: field {target.id} has a str default")
                if (
                    isinstance(value, ast.Call)
                    and getattr(value.func, "id", getattr(value.func, "attr", "")) == "Field"
                ):
                    for kw in value.keywords:
                        if kw.arg == "default" and _is_str_const(kw.value):
                            findings.append(f"{relpath}:{node.lineno}: field {target.id} has a str default")
                    if value.args and _is_str_const(value.args[0]):
                        findings.append(f"{relpath}:{node.lineno}: field {target.id} has a str default")
        elif isinstance(node, ast.Assign):
            # Class-level ``model_name = "x"`` style defaults.
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in DEFAULT_FORBIDDEN_NAMES:
                    if _is_str_const(node.value) and _inside_class(tree, node):
                        findings.append(f"{relpath}:{node.lineno}: field {target.id} has a str default")
    return findings


def _inside_class(tree: ast.AST, target: ast.AST) -> bool:
    for klass in ast.walk(tree):
        if isinstance(klass, ast.ClassDef) and target in klass.body:
            return True
    return False


def scan_string_constants(source: str, literals: Iterable[str] = LEGACY_LITERALS) -> List[str]:
    """String constants, docstrings included, that contain a legacy alias."""

    hits: List[str] = []
    for node in ast.walk(ast.parse(source)):
        if _is_str_const(node):
            for literal in literals:
                if literal in node.value:
                    hits.append(f"line {node.lineno}: {literal}")
    return hits


def test_no_model_default_parameters_or_fields_in_backend():
    findings: List[str] = []
    for path in _backend_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        findings.extend(scan_model_defaults(path.read_text(encoding="utf-8"), rel))
    assert findings == [], "\n".join(findings)


@pytest.mark.parametrize(
    "planted",
    [
        'def f(model_name: str = "x"): ...',
        'def f(*, model_id="x"): ...',
        'async def f(self, a, model: str = "x"): ...',
        'class C:\n    model_name: str = "mlx-model"',
        'class C(BaseModel):\n    coordinator_model: Optional[str] = "x"',
        'class C(BaseModel):\n    model_path: str = Field(default="x")',
        'class C:\n    routing_model = "x"',
    ],
)
def test_planted_model_defaults_are_caught(planted):
    assert scan_model_defaults(planted), planted


@pytest.mark.parametrize(
    "fine",
    [
        "def f(model_name: str): ...",
        "def f(model_name: Optional[str] = None): ...",
        'def f(mode: str = "x"): ...',
        "class C:\n    model_name: str",
        'class C:\n    note: str = "x"',
    ],
)
def test_clean_model_signatures_are_not_flagged(fine):
    assert scan_model_defaults(fine) == [], fine


def test_no_legacy_model_literals_in_backend_python():
    findings: List[str] = []
    for path in _backend_python_files():
        hits = scan_string_constants(path.read_text(encoding="utf-8"))
        findings.extend(f"{path.relative_to(REPO_ROOT).as_posix()} {hit}" for hit in hits)
    assert findings == [], "\n".join(findings)


def _swift_string_literals(source: str) -> List[str]:
    return re.findall(r'"((?:[^"\\\n]|\\.)*)"', source)


def scan_swift_literals(source: str, literals: Iterable[str] = LEGACY_LITERALS) -> List[str]:
    hits = []
    for text in _swift_string_literals(source):
        for literal in literals:
            if literal in text:
                hits.append(literal)
    return hits


def test_no_legacy_model_literals_in_swift():
    findings: List[str] = []
    for path in _swift_files():
        hits = scan_swift_literals(path.read_text(encoding="utf-8"))
        findings.extend(f"{path.relative_to(REPO_ROOT).as_posix()}: {hit}" for hit in hits)
    assert findings == [], "\n".join(findings)


def test_planted_legacy_literals_are_caught():
    for literal in LEGACY_LITERALS:
        python_source = f'def f():\n    """Docstring mentioning {literal}x."""\n    return 1\n'
        assert scan_string_constants(python_source), literal
        assert scan_swift_literals(f'let s = "{literal}suffix"'), literal


def test_model_config_module_is_gone_and_unimported():
    assert not (BACKEND / "model_config.py").exists()
    importers: List[str] = []
    for path in _backend_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("model_config", "backend.model_config"):
                importers.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("model_config", "backend.model_config"):
                        importers.append(f"{path.name}:{node.lineno}")
    assert importers == []
    # Tests and scripts too: nothing imports the deleted module.
    stale: List[str] = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from|import)\s+(backend\.)?model_config\b", text, flags=re.MULTILINE):
            stale.append(path.relative_to(REPO_ROOT).as_posix())
    assert stale == []
