"""GATE-01 static guarantee: no SSH command in the repo can skip IAP.

Every logical line (backslash continuations joined) that runs `gcloud compute
ssh` or `gcloud compute scp` must carry the IAP flag and must not use the
internal-IP flag. No line may tunnel the daily identity to port 22 with
`start-iap-tunnel`, except lines carrying the explicit negative-probe marker
(the daily-access check's own port-22 attempt).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve().relative_to(ROOT).as_posix()
SUFFIXES = (".md", ".sh", ".py", ".txt", ".service", ".json", ".toml", ".yml", ".yaml")

# Built from fragments so this file never contains a command line it would flag.
SSH_CMD = " ".join(("gcloud", "compute", "ssh"))
SCP_CMD = " ".join(("gcloud", "compute", "scp"))
IAP_FLAG = "--tunnel-through-iap"
INTERNAL_FLAG = "--internal-ip"
TUNNEL_CMD = "start-iap-tunnel"
TUNNEL_SSH_PORT = re.compile(r"start-iap-tunnel\s+\S+\s+22\b")
NEGATIVE_MARKER = "negative-probe"


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash-continued lines. Returns (first line number, joined text)."""
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        if not buffer:
            start = number
        if raw.rstrip().endswith("\\"):
            buffer.append(raw.rstrip()[:-1])
            continue
        buffer.append(raw)
        out.append((start, " ".join(part.strip() for part in buffer)))
        buffer = []
    if buffer:
        out.append((start, " ".join(part.strip() for part in buffer)))
    return out


def scan_text(text: str) -> list[tuple[int, str]]:
    offences = []
    for number, line in logical_lines(text):
        if SSH_CMD in line or SCP_CMD in line:
            if IAP_FLAG not in line:
                offences.append((number, "ssh or scp without the iap flag"))
            if INTERNAL_FLAG in line:
                offences.append((number, "ssh or scp with the internal-ip flag"))
        if TUNNEL_CMD in line and TUNNEL_SSH_PORT.search(line) and NEGATIVE_MARKER not in line:
            offences.append((number, "tunnel to the ssh port"))
    return offences


def listed_files() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise AssertionError("the IAP-only scan needs git; it fails rather than skips") from exc
    return [line for line in proc.stdout.splitlines() if line]


def test_no_ssh_command_in_the_repo_bypasses_iap():
    offences = []
    for rel in listed_files():
        if rel == THIS_FILE or not rel.endswith(SUFFIXES):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offences.extend(f"{rel}:{number}: {reason}" for number, reason in scan_text(text))
    assert offences == [], "IAP-only offences:\n" + "\n".join(offences)


# ------------------------------------------------- the scan can fail (planted)


def test_scan_flags_ssh_without_iap():
    assert scan_text(f"{SSH_CMD} vm --zone=z\n") == [(1, "ssh or scp without the iap flag")]
    assert scan_text(f"{SCP_CMD} f vm:/tmp\n") == [(1, "ssh or scp without the iap flag")]


def test_scan_accepts_ssh_with_iap_even_across_continuations():
    text = f"{SSH_CMD} vm \\\n  --zone=z \\\n  {IAP_FLAG}\n"
    assert scan_text(text) == []


def test_scan_flags_the_flag_on_a_different_logical_line():
    text = f"{SSH_CMD} vm --zone=z\necho {IAP_FLAG}\n"
    assert scan_text(text) == [(1, "ssh or scp without the iap flag")]


def test_scan_flags_internal_ip():
    text = f"{SSH_CMD} vm {IAP_FLAG} {INTERNAL_FLAG}\n"
    assert scan_text(text) == [(1, "ssh or scp with the internal-ip flag")]


def test_scan_flags_tunnel_to_port_22_unless_marked():
    bad = f"gcloud compute {TUNNEL_CMD} vm 22 --zone=z\n"
    assert scan_text(bad) == [(1, "tunnel to the ssh port")]
    assert scan_text(f"gcloud compute {TUNNEL_CMD} vm 8443 --zone=z\n") == []
    assert scan_text(f"# {NEGATIVE_MARKER}: gcloud compute {TUNNEL_CMD} vm 22\n") == []
    # port 2222 is not port 22
    assert scan_text(f"gcloud compute {TUNNEL_CMD} vm 2222\n") == []


def test_scan_reports_the_first_line_of_a_continued_command():
    text = f"echo hi\n{SSH_CMD} vm \\\n  --zone=z\n"
    assert scan_text(text) == [(2, "ssh or scp without the iap flag")]


def test_scan_fails_when_git_is_unavailable(monkeypatch):
    import pytest

    monkeypatch.setenv("PATH", "")
    with pytest.raises(AssertionError):
        listed_files()
