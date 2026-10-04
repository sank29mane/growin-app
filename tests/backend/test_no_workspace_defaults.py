"""Static guard: no identity defaults in the execution path, and Python never reads the Keychain.

ISO-01: "Remove defaults for workspace, account, and broker." A default is a way
for India and UK to mix without anyone choosing it, so CI refuses one anywhere in
the execution path. The detector proves itself on a planted source string so the
guard cannot pass vacuously.
"""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IDENTITY_NAMES = {"workspace", "account", "broker"}

# Files that decide who an order belongs to. backend/market_data is not scanned:
# Phase 59 owns it and its India-only Literal field is a type, not a fallback.
SCANNED = (
    sorted(ROOT.glob("backend/execution/*.py"))
    + [
        ROOT / "backend/app_context.py",
        ROOT / "backend/server.py",
        ROOT / "backend/routes/ai_routes.py",
        ROOT / "backend/schemas.py",
        ROOT / "backend/utils/audit_log.py",
        ROOT / "backend/workspace_credentials.py",
    ]
    + sorted(ROOT.glob("backend/private_config/*.py"))
)

# The single deliberate default: legacy audit lines carry no workspace and must
# still parse and verify. The scan fails if this entry goes stale.
ALLOW_LIST = {("backend/utils/audit_log.py", "AuditEntry", "workspace")}

KEYCHAIN_MARKERS = (
    "import keyring",
    "from keyring",
    "find-generic-password",
    "SecItemCopyMatching",
)


_REQUIRED_MARKERS = {"Field", "Query", "Body", "Path", "Header", "Form"}


def _is_required_field(value: ast.expr) -> bool:
    """True for ``Field(...)`` or ``Query(...)`` with an Ellipsis and no default.

    These pydantic and FastAPI markers declare a required value, not a fallback.
    """

    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, (ast.Name, ast.Attribute))
        and getattr(value.func, "id", getattr(value.func, "attr", "")) in _REQUIRED_MARKERS
    ):
        return False
    if not value.args or not (
        isinstance(value.args[0], ast.Constant) and value.args[0].value is Ellipsis
    ):
        return False
    return not any(keyword.arg in {"default", "default_factory"} for keyword in value.keywords)


def find_identity_defaults(source: str) -> list[tuple[str, str, str, int]]:
    """Return (kind, owner, name, line) for each identity default in ``source``."""

    tree = ast.parse(source)
    findings: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        # (a) a parameter named workspace/account/broker that has a default
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            defaulted = positional[len(positional) - len(args.defaults):] if args.defaults else []
            positional_defaults = args.defaults
            pairs = list(zip(defaulted, positional_defaults)) + list(
                zip(args.kwonlyargs, args.kw_defaults)
            )
            for argument, default in pairs:
                if (
                    default is not None
                    and argument.arg in IDENTITY_NAMES
                    and not _is_required_field(default)
                ):
                    findings.append(("parameter-default", node.name, argument.arg, node.lineno))
        if isinstance(node, ast.Call):
            function = node.func
            # (b) mapping.get("workspace" | "account" | "broker", fallback)
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in IDENTITY_NAMES
            ):
                findings.append(("get-fallback", "get", str(node.args[0].value), node.lineno))
            # (c) getenv / environ.get of GROWIN_WORKSPACE with a second argument
            name = getattr(function, "attr", getattr(function, "id", ""))
            if (
                name in {"getenv", "get"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "GROWIN_WORKSPACE"
                and (len(node.args) > 1 or node.keywords)
            ):
                findings.append(("env-default", name, "GROWIN_WORKSPACE", node.lineno))
        # (d) a class field named workspace/account/broker with a value
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if (
                    isinstance(child, ast.AnnAssign)
                    and isinstance(child.target, ast.Name)
                    and child.target.id in IDENTITY_NAMES
                    and child.value is not None
                    and not _is_required_field(child.value)
                ):
                    findings.append(("field-default", node.name, child.target.id, child.lineno))
    return findings


PLANTED = '''
import os

def place(workspace="uk"):
    return workspace

proposal = {}
account = proposal.get("account", "invest")
chosen = os.getenv("GROWIN_WORKSPACE", "uk")

class Model(BaseModel):
    broker: str = "trading212"
    ok_required: str = Field(..., min_length=1)
    workspace: Workspace = Field(...)
    account: str
'''


def test_detector_reports_every_planted_violation():
    findings = find_identity_defaults(PLANTED)

    assert {kind for kind, *_ in findings} == {
        "parameter-default",
        "get-fallback",
        "env-default",
        "field-default",
    }
    assert len(findings) == 4


def test_detector_accepts_required_fields_and_default_free_calls():
    clean = '''
import os

def place(*, workspace, account, broker):
    return os.environ.get("GROWIN_WORKSPACE")

def status(workspace: Workspace = Query(...)):
    return workspace

class Model(BaseModel):
    workspace: Workspace
    account: str = Field(..., min_length=1)
    broker: str = Field(..., min_length=1)
'''

    assert find_identity_defaults(clean) == []


def test_scanned_files_exist():
    missing = [str(path.relative_to(ROOT)) for path in SCANNED if not path.is_file()]

    assert missing == []
    assert len(SCANNED) >= 12


def test_execution_path_has_no_workspace_account_or_broker_defaults():
    seen: set[tuple[str, str, str]] = set()
    unexpected: list[str] = []
    for path in SCANNED:
        relative = str(path.relative_to(ROOT))
        for kind, owner, name, line in find_identity_defaults(path.read_text(encoding="utf-8")):
            key = (relative, owner, name)
            seen.add(key)
            if key not in ALLOW_LIST:
                unexpected.append(f"{relative}:{line} {kind} {owner}.{name}")

    assert unexpected == []
    stale = ALLOW_LIST - seen
    assert not stale, f"allow-list entries no longer exist: {sorted(stale)}"


def test_python_never_reads_the_macos_keychain():
    try:
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "backend"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.fail(f"cannot list tracked backend files: {exc}")

    tracked = [ROOT / line for line in listing.splitlines() if line.endswith(".py")]
    assert tracked, "git ls-files returned no backend Python files"
    offenders = []
    for path in tracked:
        if path.resolve() == Path(__file__).resolve() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders.extend(
            f"{path.relative_to(ROOT)}: {marker}" for marker in KEYCHAIN_MARKERS if marker in text
        )

    assert offenders == []
