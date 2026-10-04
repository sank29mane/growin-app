"""The Safety Guard path list keeps its old entries and guards the Phase 58 controls.

Patterns are matched with the same bash test .github/scripts/safety-guard.sh
uses: ``[[ "$path" == $pattern ]]``, where ``*`` also crosses ``/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SAFETY_PATHS = REPO_ROOT / ".github" / "safety-paths.txt"

EXISTING_ENTRIES = (
    "backend/execution/*",
    "backend/brokers/*",
    "backend/market_data/*",
    "backend/simulation/*",
    "backend/costs/*",
    "backend/app_context.py",
    "backend/trading_loop.py",
    "backend/trading212_mcp_server.py",
    "backend/t212_handlers.py",
    "backend/shared_types.py",
    "backend/security_middleware.py",
    "backend/agents/risk_agent.py",
    "backend/agents/decision_agent.py",
    "backend/utils/risk_engine.py",
    "backend/lm_studio_client.py",
    "backend/routes/mcp_routes.py",
    "backend/routes/ai_routes.py",
    "backend/routes/market_routes.py",
    "backend/routes/market_data_routes.py",
    "Growin/Security/*",
    ".github/*",
)

# (pattern, a sample path it must match)
PHASE_58_ENTRIES = (
    ("backend/private_config/*", "backend/private_config/loader.py"),
    ("backend/server.py", "backend/server.py"),
    ("backend/utils/audit_log.py", "backend/utils/audit_log.py"),
    ("backend/workspace_credentials.py", "backend/workspace_credentials.py"),
    ("scripts/ledger_tool.py", "scripts/ledger_tool.py"),
    ("private/*", "private/india/limits.json"),
    ("tests/backend/test_private_config.py", "tests/backend/test_private_config.py"),
)


def entries() -> list[str]:
    lines = SAFETY_PATHS.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.startswith("#")]


def bash_matches(path: str, pattern: str) -> bool:
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to check Safety Guard pattern semantics")
    result = subprocess.run(
        [bash, "-c", '[[ "$1" == $2 ]]', "_", path, pattern],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_existing_entries_are_still_present():
    present = entries()
    assert len(EXISTING_ENTRIES) == 21
    for entry in EXISTING_ENTRIES:
        assert entry in present, f"{entry} was removed from .github/safety-paths.txt"


def test_phase_58_entries_are_present_in_order_at_the_end():
    present = entries()
    expected = [pattern for pattern, _ in PHASE_58_ENTRIES]
    assert present[-len(expected):] == expected
    assert present[: len(EXISTING_ENTRIES)] == list(EXISTING_ENTRIES), "existing lines were reordered"
    assert "# Phase 58:" in SAFETY_PATHS.read_text(encoding="utf-8")


@pytest.mark.parametrize(("pattern", "sample"), PHASE_58_ENTRIES)
def test_each_new_pattern_matches_its_sample_the_way_the_guard_does(pattern, sample):
    assert pattern in entries()
    assert bash_matches(sample, pattern)


def test_private_config_test_is_safety_guarded():
    path = "tests/backend/test_private_config.py"
    assert path in entries()
    assert any(bash_matches(path, pattern) for pattern in entries())


def test_gitignore_is_not_safety_guarded():
    # Operator decision: the guarded test, not .gitignore, catches a removed rule.
    assert ".gitignore" not in entries()
    assert not any(bash_matches(".gitignore", pattern) for pattern in entries())


def test_private_config_keeps_gitignore_checks():
    text = (REPO_ROOT / "tests" / "backend" / "test_private_config.py").read_text(encoding="utf-8")
    assert "check-ignore" in text
    assert "ls-files" in text
