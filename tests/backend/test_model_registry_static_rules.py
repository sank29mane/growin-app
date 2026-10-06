"""Review round 1: stricter AST rules against model-name guessing (AC-8, AC-9).

``test_model_registry_static.py`` catches comparisons between a string constant
and a model or provider expression. These rules close the gaps found in review:

* R1  a string literal passed as ``model=``, ``model_name=``, ``model_id=`` or
      ``llm_id=``, a literal first argument to a model client constructor, a
      literal assigned to a model variable or attribute, a dict with a literal
      ``"model"``/``"model_name"`` value, ``**{"model": ...}`` into a client
      constructor, a literal parameter default on a model-named parameter, and a
      ``getenv``/``dict.get``/``getattr`` fallback literal for a model-named key;
* R2  a module-level string constant named like a model, or whose value looks
      like a model id;
* R3  any comparison, ``match`` statement, ``startswith``/``endswith``/``find``,
      ``or "literal"`` default, dict lookup (``X[model]``, ``X.get(model)``) on a
      variable named ``model``, ``model_name``, ``model_id``, ``model_path`` or
      ``llm_id``, including local aliases of one. ``len(model) > LIMIT`` against
      a numeric constant is not a name comparison and is ignored;
* R4  a magentic prompt function (``@prompt``, ``@mag_prompt``, ``@chatprompt``)
      used anywhere except as the function argument of ``run_magentic``.

R1 to R3 scan the LLM runtime surface (``SURFACE_GLOBS``); R4 scans all of
``backend/``, because a bare magentic call is wrong anywhere. Each exception is
in ``ALLOWLIST`` with its reason. Every rule has planted-source tests proving it
fires.
"""

import ast
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

MODEL_VARS = {"model", "model_name", "model_id", "model_path", "llm_id"}
MODEL_KWARGS = {"model", "model_name", "model_id", "llm_id"}
MODEL_CONSTRUCTORS = {
    "ChatOpenAI",
    "OpenaiChatModel",
    "OpenAIModel",
    "ChatAnthropic",
    "ChatGoogleGenerativeAI",
    "ChatOllama",
    "ChatMLX",
}
STRING_PREDICATES = {"startswith", "endswith", "find", "rfind", "index", "count", "__contains__"}
PROMPT_DECORATORS = {"prompt", "mag_prompt", "chatprompt", "mag_chatprompt"}
RUN_MAGENTIC = "run_magentic"

MODEL_FAMILY = re.compile(
    r"(gpt-?\d|claude|gemini|grok|llama|mistral|mixtral|gemma|qwen|granite|nemotron|"
    r"deepseek|lfm|phi-?\d|o[134]-(mini|preview)|text-embedding|whisper)",
    re.IGNORECASE,
)

# The LLM runtime surface for R1 to R3. Not scanned, by decision: market-data
# providers (utils/data_frayer.py), backtest_lab/, models/clara/, the cost model.
SURFACE_GLOBS = (
    "backend/agents/*.py",
    "backend/model_registry/*.py",
    "backend/routes/chat_routes.py",
    "backend/routes/agent_routes.py",
    "backend/routes/ai_routes.py",
    "backend/routes/market_routes.py",
    "backend/app_context.py",
    "backend/server.py",
    "backend/chat_manager.py",
    "backend/lm_studio_client.py",
    "backend/mlx_engine.py",
    "backend/mlx_vlm_engine.py",
    "backend/mlx_langchain.py",
    "backend/forecaster.py",
    "backend/forecast_bridge.py",
    "backend/status_manager.py",
    "backend/utils/worker_client.py",
    "backend/utils/worker_service.py",
    "backend/utils/rstitch_engine.py",
)

# Explicit allow-list: (repo-relative path, rule, EXACT flagged source text) ->
# reason. An entry exempts only that comparison in that file; a new hit with
# different text, or in another file, is reported.
ALLOWLIST: Dict[Tuple[str, str, str], str] = {
    ("backend/mlx_engine.py", "R3", '"gemma-4" in model_path.lower()'): (
        "deferred by the phase brief (mlx_engine.py:142): in-process MLX checkpoint "
        "path handling is not a registry role"
    ),
    ("backend/mlx_engine.py", "R3", '"vlm" in model_path.lower()'): (
        "deferred by the phase brief (mlx_engine.py:142): same line, second operand"
    ),
    ("backend/mlx_vlm_engine.py", "R3", '"/" in model_path'): (
        "deferred by the phase brief (mlx_vlm_engine.py:44): VLM checkpoint path "
        "handling is not a registry role"
    ),
    ("backend/routes/market_routes.py", "R3", 'model_name.split(" ")[0].lower() in (algorithm or "").lower()'): (
        "display label (market_routes.py:625): matches a printed algorithm name in a "
        "forecast response; it selects no model"
    ),
    ("backend/forecaster.py", "R1", '"XGBoost (ML)"'): (
        "display label of the built-in XGBoost baseline in auxiliary_forecasts; "
        "not a registry model and not a model selection"
    ),
    ("backend/forecaster.py", "R1", '"Holt-Winters (Statistical)"'): (
        "display label of the built-in Holt-Winters baseline in auxiliary_forecasts; "
        "not a registry model and not a model selection"
    ),
    ("backend/routes/market_routes.py", "R1", 'a.get("model", "Unknown")'): (
        "display fallback next to market_routes.py:625 when an auxiliary forecast has no label"
    ),
    ("backend/lm_studio_client.py", "R3", "model_id in loaded"): (
        "operator LM Studio management tool (load/unload): membership check of the "
        "operator's chosen id against LM Studio's own loaded list; the brief keeps "
        "this file untouched and it selects no runtime role"
    ),
    ("backend/routes/agent_routes.py", "R3", "model_id in loaded_models"): (
        "operator route /api/models/lmstudio/load: the same loaded-list membership "
        "check for the LM Studio management tool; it selects no runtime role"
    ),
}


# --- helpers ----------------------------------------------------------------------------


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_none_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _model_var_name(node: ast.AST, aliases: Set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in MODEL_VARS or node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr in MODEL_VARS
    return False


def _involves_model_var(node: ast.AST, aliases: Set[str]) -> bool:
    return any(_model_var_name(child, aliases) for child in ast.walk(node))


def _derives_from_model(node: ast.AST, aliases: Set[str]) -> bool:
    """True when ``node`` is the model variable or a plain transformation of it.

    ``(self.model_name or "").lower()`` derives from the variable; passing the
    variable as an argument to some other call (``load(model_path)``) does not.
    """

    if _model_var_name(node, aliases):
        return True
    if isinstance(node, ast.BoolOp):
        return any(_derives_from_model(v, aliases) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _derives_from_model(node.body, aliases) or _derives_from_model(node.orelse, aliases)
    if isinstance(node, ast.Subscript):
        return _derives_from_model(node.value, aliases)
    if isinstance(node, ast.JoinedStr):
        return False
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return _derives_from_model(node.func.value, aliases)
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "lower"} and node.args:
            return _derives_from_model(node.args[0], aliases)
    return False


def _collect_aliases(tree: ast.AST) -> Set[str]:
    """Local names assigned from a plain transformation of a model variable."""

    aliases: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _derives_from_model(node.value, aliases):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    return aliases


_CONSTANT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_numeric_limit(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return True
    return isinstance(node, ast.Name) and bool(_CONSTANT_NAME.match(node.id))


def _is_length_limit_check(node: ast.Compare, aliases: Set[str]) -> bool:
    """``len(<model var>) <op> LIMIT``: a size check, not a comparison of names."""

    if len(node.comparators) != 1:
        return False
    left, right = node.left, node.comparators[0]
    for measured, limit in ((left, right), (right, left)):
        if (
            isinstance(measured, ast.Call)
            and isinstance(measured.func, ast.Name)
            and measured.func.id == "len"
            and len(measured.args) == 1
            and _involves_model_var(measured.args[0], aliases)
            and _is_numeric_limit(limit)
        ):
            return True
    return False


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _segment(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


# --- rules ------------------------------------------------------------------------------


def scan_literals_and_comparisons(source: str, relpath: str = "<planted>") -> List[Tuple[str, int, str]]:
    """Rules R1, R2 and R3. Returns (rule, line, source segment)."""

    tree = ast.parse(source)
    aliases = _collect_aliases(tree)
    found: List[Tuple[str, int, str]] = []

    def add(rule: str, node: ast.AST) -> None:
        found.append((rule, node.lineno, _segment(source, node)))

    # R2: module-level constants.
    for node in tree.body:
        targets: List[ast.AST] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or not _is_str_const(value):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                named_like_model = "MODEL" in target.id.upper()
                valued_like_model = bool(MODEL_FAMILY.search(value.value)) and " " not in value.value and "://" not in value.value
                if named_like_model or valued_like_model:
                    add("R2", node)

    for node in ast.walk(tree):
        # R1: literal model kwargs, constructor arguments, assignments.
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in MODEL_KWARGS and _is_str_const(kw.value) and kw.value.value:
                    add("R1", node)
            if _call_name(node.func) in MODEL_CONSTRUCTORS and node.args and _is_str_const(node.args[0]):
                add("R1", node)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is not None and _is_str_const(key) and key.value in MODEL_KWARGS and _is_str_const(value) and value.value:
                    add("R1", value)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Dict) and _call_name(node.func) in MODEL_CONSTRUCTORS:
                    if any(k is not None and _is_str_const(k) and k.value in MODEL_KWARGS for k in kw.value.keys):
                        add("R1", node)
            name = _call_name(node.func)
            args = node.args
            if name in {"getenv", "get"} and len(args) >= 2:
                if _is_str_const(args[0]) and "model" in args[0].value.lower() and _is_str_const(args[1]) and args[1].value:
                    add("R1", node)
            if name == "getattr" and len(args) >= 3:
                if _is_str_const(args[1]) and "model" in args[1].value.lower() and _is_str_const(args[2]) and args[2].value:
                    add("R1", node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spec = node.args
            positional = list(spec.posonlyargs) + list(spec.args)
            for arg, default in zip(positional[len(positional) - len(spec.defaults):], spec.defaults):
                if arg.arg in MODEL_VARS and _is_str_const(default) and default.value:
                    add("R1", default)
            for arg, default in zip(spec.kwonlyargs, spec.kw_defaults):
                if default is not None and arg.arg in MODEL_VARS and _is_str_const(default) and default.value:
                    add("R1", default)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and _is_str_const(value) and value.value:
                for target in targets:
                    if _model_var_name(target, set()):
                        add("R1", node)
        # R3: comparisons and string predicates on a model variable.
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_involves_model_var(op, aliases) for op in operands):
                identity_only = all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
                none_check = any(_is_none_const(op) for op in operands)
                if not identity_only and not none_check and not _is_length_limit_check(node, aliases):
                    add("R3", node)
        if isinstance(node, ast.Match) and _involves_model_var(node.subject, aliases):
            add("R3", node.subject)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in STRING_PREDICATES and _involves_model_var(node.func.value, aliases):
                add("R3", node)
            if node.func.attr == "get" and node.args and _involves_model_var(node.args[0], aliases):
                add("R3", node)
        if isinstance(node, ast.Subscript) and _involves_model_var(node.slice, aliases):
            add("R3", node)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(_involves_model_var(v, aliases) for v in node.values) and any(
                _is_str_const(v) and v.value for v in node.values
            ):
                add("R3", node)
    return found


def prompt_function_names(source: str) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if _call_name(target) in PROMPT_DECORATORS:
                    names.add(node.name)
    return names


def scan_prompt_usage(source: str, prompt_names: Set[str]) -> List[Tuple[str, int, str]]:
    """Rule R4: a magentic prompt function referenced outside ``run_magentic``."""

    tree = ast.parse(source)
    allowed: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) == RUN_MAGENTIC:
            for arg in node.args[1:2]:
                allowed.add(id(arg))
    defined_here = {
        id(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in prompt_names
    }
    found: List[Tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if id(node) in defined_here:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in prompt_names:
            if id(node) not in allowed:
                found.append(("R4", node.lineno, _segment(source, node)))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in prompt_names
            and isinstance(node.value, ast.Name)
            and node.value.id != "self"
            and id(node) not in allowed
        ):
            found.append(("R4", node.lineno, _segment(source, node)))
    return found


# --- scanning the tree ---------------------------------------------------------------------


def backend_files() -> List[Path]:
    """Every backend Python file (R4 scope)."""
    files = []
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.relative_to(BACKEND).parts
        if ".venv" in parts or "__pycache__" in parts:
            continue
        files.append(path)
    return files


def surface_files() -> List[Path]:
    """The LLM runtime surface (R1 to R3 scope)."""
    files: List[Path] = []
    for pattern in SURFACE_GLOBS:
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    return files


def _allowed(relpath: str, rule: str, segment: str) -> bool:
    """Exact match on file, rule and flagged text."""
    return (relpath, rule, segment.strip()) in ALLOWLIST


def surface_findings(relpath: str, source: str) -> List[str]:
    """R1 to R3 findings for one surface file, allow-list applied."""
    return [
        f"{relpath}:{line}: {rule}: {segment[:100]!r}"
        for rule, line, segment in scan_literals_and_comparisons(source, relpath)
        if not _allowed(relpath, rule, segment)
    ]


def scan_tree() -> List[str]:
    sources = {p: p.read_text(encoding="utf-8") for p in backend_files()}
    surface = set(surface_files())
    prompt_names: Set[str] = set()
    for source in sources.values():
        prompt_names |= prompt_function_names(source)
    findings: List[str] = []
    for path, source in sources.items():
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits = scan_prompt_usage(source, prompt_names)
        if path in surface:
            hits += scan_literals_and_comparisons(source, rel)
        for rule, line, segment in hits:
            # R2 inside the registry package is not exempt: it holds no model names.
            if not _allowed(rel, rule, segment):
                findings.append(f"{rel}:{line}: {rule}: {segment[:100]!r}")
    return findings


def test_the_scan_sees_the_tree_and_the_prompt_functions():
    names = {p.name for p in backend_files()}
    assert {"decision_agent.py", "risk_agent.py", "chat_routes.py", "worker_client.py"} <= names
    prompts: Set[str] = set()
    for path in backend_files():
        prompts |= prompt_function_names(path.read_text(encoding="utf-8"))
    assert {"extract_tool_calls", "conduct_risk_audit", "generate_news_query"} <= prompts
    assert "analyze_portfolio_quality" not in prompts


def test_no_model_guessing_literals_constants_comparisons_or_bare_magentic_calls():
    findings = scan_tree()
    assert findings == [], "\n".join(findings)


def test_every_allowlist_entry_has_a_reason_and_still_matches_something():
    sources = {p.relative_to(REPO_ROOT).as_posix(): p.read_text(encoding="utf-8") for p in surface_files()}
    for (path, rule, needle), reason in ALLOWLIST.items():
        assert reason.strip(), (path, rule)
        assert path in sources, f"allow-listed file is gone: {path}"
    # A stale entry (the code it excused was fixed) must be removed, not left behind.
    for (path, rule, needle), _reason in ALLOWLIST.items():
        hits = [
            segment
            for r, _line, segment in scan_literals_and_comparisons(sources[path], path)
            if r == rule and segment.strip() == needle
        ]
        assert hits, f"stale allow-list entry: {(path, rule, needle)}"


# --- planted cases: every rule fires -----------------------------------------------------------

PLANTED_R1 = [
    ('llm = ChatOpenAI(model="grok-4")', "R1"),
    ('llm = ChatOllama(model_name="x")', "R1"),
    ('call(llm_id="some-id")', "R1"),
    ('client = OpenaiChatModel("gpt-4o-mini")', "R1"),
    ('self.model_name = "my-model"', "R1"),
]
PLANTED_R2 = [
    ('DEFAULT_MODEL = "llama-3.1-8b"', "R2"),
    ('FALLBACK = "grok-4-fast"', "R2"),
    ('SMALL_MODEL: str = "anything"', "R2"),
    ('HUB = "some-org/qwen2.5-7b"', "R2"),
]
PLANTED_R3 = [
    ('ok = "tiny" in name\nname = (self.model_name or "").lower()', "R3"),
    ('ok = llm_id.startswith("gpt")', "R3"),
    ('ok = MODEL_X == self.model_name', "R3"),
    ('caps = CAPS.get(self.model_name)', "R3"),
    ('caps = CAPS[model_id]', "R3"),
    ('x = request.model_name or "fallback-model"', "R3"),
    ('ok = model.endswith("-mini")', "R3"),
    ('ok = model_name != other', "R3"),
    ('ok = len(model) > limit', "R3"),
    ('ok = len(model) == len(other_model_name)', "R3"),
    ('ok = len(model) > 4 and model == "x"', "R3"),
]
PLANTED_GAP_B = [
    ('m = os.getenv("X_MODEL", "grok-4")', "R1"),
    ('cfg = {"model": "gpt-4o"}', "R1"),
    ('cfg = {"model_name": "mistral"}', "R1"),
    ('match model_name:\n    case "gpt-4o":\n        pass', "R3"),
    ('def build(prompt, model="llama-3.1-8b"): ...', "R1"),
    ('async def build(prompt, *, model_name="mistral"): ...', "R1"),
    ('m = getattr(s, "model_name", "mistral")', "R1"),
    ('llm = ChatOpenAI(**{"model": resolved_name})', "R1"),
    ('m = settings.get("model", "gemma")', "R1"),
]
PLANTED_R4 = [
    ('@mag_prompt("hi")\ndef ask(x: str) -> str: ...\nresult = ask("a")', "R4"),
    ('@prompt("hi")\ndef ask(x: str) -> str: ...\nasyncio.to_thread(ask, "a")', "R4"),
    ('@chatprompt()\ndef ask(x: str) -> str: ...\nvalue = mod.ask("a")', "R4"),
]


def _scan_all(source: str):
    return scan_literals_and_comparisons(source) + scan_prompt_usage(source, prompt_function_names(source))


@pytest.mark.parametrize("planted,rule", PLANTED_R1 + PLANTED_R2 + PLANTED_R3 + PLANTED_GAP_B + PLANTED_R4)
def test_planted_case_is_caught_by_its_rule(planted, rule):
    assert rule in {r for r, _line, _seg in _scan_all(planted)}, planted


@pytest.mark.parametrize(
    "clean",
    [
        "ok = model is None",
        "ok = model_name is not None",
        "ok = self.model_name == None",
        "status_manager.set_status('a', 'b', model=self.model_name)",
        "self.model_name = model_name",
        "payload = {'model': self.model_name}",
        "ROLE = 'decision'",
        "MAX_MODEL_ID_CHARS = 256",
        "LABEL = 'plain text'",
        "ok = role == 'decision'",
        "x = CAPS.get(role)",
        "ok = provider is KIND",
        '@mag_prompt("hi")\ndef ask(x: str) -> str: ...\nvalue = await run_magentic("decision", ask, "a")',
        "client = ChatOpenAI(model=resolved.model)",
        "bad = len(model) > MAX_MODEL_ID_CHARS",
        "bad = len(self.model_name) > 256",
        'v = os.getenv("LOG_LEVEL", "info")',
        'x = getattr(obj, "name", "plain")',
        'cfg = {"role": "decision"}',
        "def build(prompt, model=None): ...",
        "match role:\n    case 'decision':\n        pass",
    ],
)
def test_ordinary_code_is_not_flagged_by_the_new_rules(clean):
    assert _scan_all(clean) == [], clean


def test_allowlist_matches_only_its_own_file_rule_and_text():
    text = '"vlm" in model_path.lower()'
    assert _allowed("backend/mlx_engine.py", "R3", text)
    assert not _allowed("backend/other.py", "R3", text)
    assert not _allowed("backend/mlx_engine.py", "R1", text)
    assert not _allowed("backend/mlx_engine.py", "R3", '"qwen" in model_path.lower()')
    # Exact text, not a substring: a longer or different expression is not exempt.
    assert not _allowed("backend/mlx_engine.py", "R3", text + " or True")


def test_a_new_hit_in_an_allow_listed_file_is_not_exempted():
    path = BACKEND / "mlx_engine.py"
    source = path.read_text(encoding="utf-8")
    assert surface_findings("backend/mlx_engine.py", source) == []
    for added in (
        'if "qwen" in model_path.lower():\n    pass\n',
        'if model_path == other:\n    pass\n',
        'DEFAULT_MODEL = "llama-3.1-8b"\n',
    ):
        findings = surface_findings("backend/mlx_engine.py", source + "\n" + added)
        assert findings, added
    source = (BACKEND / "mlx_vlm_engine.py").read_text(encoding="utf-8")
    assert surface_findings("backend/mlx_vlm_engine.py", source + '\nx = "abc" in model_path\n')
