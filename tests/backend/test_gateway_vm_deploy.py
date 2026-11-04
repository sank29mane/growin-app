"""Deploy artifacts for the VM relay (Phase 61-11, GATE-01, GATE-03).

Covers gateway/vm/pyproject.toml, deploy/growin-gateway.service, deploy/relay.example.json and
deploy/RUNBOOK.md. The unit is parsed (repeated keys kept) and every line is pinned, so a loosened
hardening key, a new Environment entry or a changed path turns a row red. A local namespace
simulation (D13) builds the file tree the unit would show the service and loads the relay's own
loaders over it. Real systemd behaviour (TemporaryFileSystem with BindReadOnlyPaths, LoadCredential
ownership) and MemoryDenyWriteExecute with the native extensions cannot be settled here; they are
deploy checks in RUNBOOK.md section 6.

Rows D1 and the lock half of D7 need gateway/vm/uv.lock, which is plan 61-11 Task 1b (blocked on
the operator's packages answer). They skip with that reason until the lock lands. Helper functions
return problem lists so a negative control can assert that a broken input is caught.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import config as relay_config  # noqa: E402
from gateway_vm.breeze_client import BreezeClient  # noqa: E402
from gateway_vm.counter_store import FileCounterStore  # noqa: E402
from gateway_vm.egress import EgressChecker  # noqa: E402
from gateway_vm.events import EventLog  # noqa: E402
from gateway_vm.orders.routes import OrderSettings, build_order_service  # noqa: E402
from gateway_vm.session import Credentials, SessionManager  # noqa: E402
from test_gateway_relay_app import (  # noqa: E402
    APP_KEY,
    EXPECTED_IP,
    SECRET_KEY,
    USERID,
    BudgetDouble,
    FakeClock,
    FakeTransport,
)

VM = ROOT / "gateway" / "vm"
DEPLOY = VM / "deploy"
UNIT_PATH = DEPLOY / "growin-gateway.service"
EXAMPLE_PATH = DEPLOY / "relay.example.json"
RUNBOOK_PATH = DEPLOY / "RUNBOOK.md"
PYPROJECT_PATH = VM / "pyproject.toml"
LOCK_PATH = VM / "uv.lock"
BACKEND_LOCK_PATH = ROOT / "backend" / "uv.lock"
FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
PRIMARY = VECTORS["keys"]["primary"]

LIMITS_PATH = relay_config.DEFAULT_LIMITS_PATH
ORDER_KEY_PATH = relay_config.DEFAULT_ORDER_KEY_PATH
ETC_DIR = "/etc/growin-gateway"
RELAY_JSON_HOST = "/etc/growin-gateway/relay.json"

# One table for the host file modes. RUNBOOK sections 5 and 6 must say the same numbers (D14).
HOST_MODES = {
    ETC_DIR: 0o700,
    RELAY_JSON_HOST: 0o600,
    LIMITS_PATH: 0o644,
    ORDER_KEY_PATH: 0o644,
}

NO_LOCK = "plan 61-11 Task 1b (gateway/vm/uv.lock) waits for the operator's packages answer (O4)"

EXPECTED_DIRECT = ["fastapi==0.109.2", "uvicorn==0.27.1", "pydantic==2.12.5", "cryptography==46.0.4"]
EXPECTED_CONSTRAINTS = [
    "starlette==0.36.3",
    "pydantic-core==2.41.5",
    "typing-inspection==0.4.2",
    "annotated-types==0.7.0",
    "typing-extensions==4.15.0",
    "anyio==4.12.1",
    "sniffio==1.3.1",
    "idna==3.11",
    "h11==0.16.0",
    "click==8.3.1",
    "cffi==2.0.0",
    "pycparser==3.0",
]
# top-level import name -> distribution that provides it
IMPORT_TO_DIST = {
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "cryptography": "cryptography",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ the unit


def parse_unit(text: str) -> list[tuple[str, str, str]]:
    """(section, key, value) in file order; repeated keys are kept. Comments and blanks are skipped."""
    entries: list[tuple[str, str, str]] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        assert sep, f"unit line without '=': {line!r}"
        entries.append((section, key.strip(), value.strip()))
    return entries


def values(entries, section: str, key: str) -> list[str]:
    return [v for s, k, v in entries if s == section and k == key]


EXPECTED_SERVICE = {
    "Type": ["simple"],
    "DynamicUser": ["yes"],
    "User": ["growin-gateway"],
    "StateDirectory": ["growin-gateway"],
    "StateDirectoryMode": ["0700"],
    "RuntimeDirectory": ["growin-gateway"],
    "RuntimeDirectoryMode": ["0700"],
    "WorkingDirectory": ["/run/growin-gateway"],
    "LoadCredential": ["relay.json:/etc/growin-gateway/relay.json"],
    "TemporaryFileSystem": ["/etc/growin-gateway:ro,mode=0755"],
    "BindReadOnlyPaths": ["-" + LIMITS_PATH, "-" + ORDER_KEY_PATH],
    "Environment": [
        "PYTHONPATH=/opt/growin-gateway/src",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "PYTHONNOUSERSITE=1",
    ],
    "ExecStart": [
        "/opt/growin-gateway/.venv/bin/python -B -s -m gateway_vm.main --host internal-ip --port 8443"
    ],
    "NoNewPrivileges": ["yes"],
    "ProtectSystem": ["strict"],
    "ProtectHome": ["yes"],
    "PrivateTmp": ["yes"],
    "PrivateDevices": ["yes"],
    "ProtectKernelTunables": ["yes"],
    "ProtectKernelModules": ["yes"],
    "ProtectKernelLogs": ["yes"],
    "ProtectControlGroups": ["yes"],
    "ProtectClock": ["yes"],
    "ProtectHostname": ["yes"],
    "ProtectProc": ["invisible"],
    "ProcSubset": ["pid"],
    "RestrictAddressFamilies": ["AF_INET AF_INET6 AF_UNIX"],
    "RestrictNamespaces": ["yes"],
    "RestrictRealtime": ["yes"],
    "RestrictSUIDSGID": ["yes"],
    "LockPersonality": ["yes"],
    "MemoryDenyWriteExecute": ["yes"],
    "SystemCallArchitectures": ["native"],
    "SystemCallFilter": ["@system-service", "~@privileged @resources"],
    "CapabilityBoundingSet": [""],
    "AmbientCapabilities": [""],
    "UMask": ["0077"],
    "LimitCORE": ["0"],
    "MemoryMax": ["512M"],
    "TasksMax": ["64"],
    "Restart": ["on-failure"],
    "RestartSec": ["5"],
}


def unit_problems(text: str) -> list[str]:
    """Everything wrong with a unit text. Empty means it is exactly the pinned unit."""
    problems: list[str] = []
    entries = parse_unit(text)
    service_keys = {k for s, k, _ in entries if s == "Service"}
    for key in sorted(service_keys - set(EXPECTED_SERVICE)):
        problems.append(f"unexpected Service key {key}")
    for key, expected in EXPECTED_SERVICE.items():
        got = values(entries, "Service", key)
        if got != expected:
            problems.append(f"Service {key}: {got!r} != {expected!r}")
    unit_expected = {
        "StartLimitIntervalSec": ["300"],
        "StartLimitBurst": ["5"],
        "After": ["network-online.target"],
        "Wants": ["network-online.target"],
    }
    for key, expected in unit_expected.items():
        if values(entries, "Unit", key) != expected:
            problems.append(f"Unit {key}")
    if values(entries, "Install", "WantedBy") != ["multi-user.target"]:
        problems.append("Install WantedBy")
    if any("--config" in v for v in values(entries, "Service", "ExecStart")):
        problems.append("ExecStart passes --config")
    for name in ("CREDENTIALS_DIRECTORY", "STATE_DIRECTORY"):
        if any(v.startswith(name + "=") for v in values(entries, "Service", "Environment")):
            problems.append(f"Environment sets {name}")
    if values(entries, "Service", "EnvironmentFile"):
        problems.append("EnvironmentFile")
    for banned in ("0.0.0.0", "127.0.0.1"):
        if banned in text:
            problems.append(f"text contains {banned}")
    return problems


def test_d3_unit_is_exactly_the_pinned_unit():
    assert unit_problems(_text(UNIT_PATH)) == []


def test_d3_bind_paths_are_the_relay_defaults():
    binds = values(parse_unit(_text(UNIT_PATH)), "Service", "BindReadOnlyPaths")
    assert [b.removeprefix("-") for b in binds] == [
        relay_config.DEFAULT_LIMITS_PATH,
        relay_config.DEFAULT_ORDER_KEY_PATH,
    ]
    assert all(b.startswith("-") for b in binds)  # optional: the unit starts before 63-06 installs them


def _mutate(old: str, new: str) -> str:
    text = _text(UNIT_PATH)
    assert old in text, f"mutation target missing: {old!r}"
    return text.replace(old, new, 1)


def lazy(old: str, new: str):
    """A mutation evaluated at test time, so a changed real unit fails the test and not the collection."""
    return lambda: _mutate(old, new)


@pytest.mark.parametrize(
    "label,make",
    [
        (
            "P5 --config on ExecStart",
            lazy("--host internal-ip", "--config /etc/growin-gateway/relay.json --host internal-ip"),
        ),
        (
            "P6 second LoadCredential",
            lazy("LoadCredential=relay.json:", "LoadCredential=other.json:/etc/growin-gateway/o.json\nLoadCredential=relay.json:"),
        ),
        ("P7 MemoryDenyWriteExecute off", lazy("MemoryDenyWriteExecute=yes", "MemoryDenyWriteExecute=no")),
        ("P8 EnvironmentFile", lazy("Environment=PYTHONUNBUFFERED=1", "EnvironmentFile=/etc/x.env\nEnvironment=PYTHONUNBUFFERED=1")),
        ("P14 TemporaryFileSystem deleted", lazy("TemporaryFileSystem=/etc/growin-gateway:ro,mode=0755\n", "")),
        ("P15 tmpfs mode 0700", lazy("ro,mode=0755", "ro,mode=0700")),
        (
            "P16 dash dropped from a bind",
            lazy("BindReadOnlyPaths=-/etc/growin-gateway/limits.json", "BindReadOnlyPaths=/etc/growin-gateway/limits.json"),
        ),
        ("credential in Environment", lazy("Environment=PYTHONUNBUFFERED=1", "Environment=BREEZE_X=1\nEnvironment=PYTHONUNBUFFERED=1")),
        ("CREDENTIALS_DIRECTORY set", lazy("Environment=PYTHONUNBUFFERED=1", "Environment=CREDENTIALS_DIRECTORY=/x\nEnvironment=PYTHONUNBUFFERED=1")),
        ("capability added", lazy("CapabilityBoundingSet=", "CapabilityBoundingSet=CAP_NET_BIND_SERVICE")),
        ("wildcard bind in a comment", lambda: _text(UNIT_PATH) + "\n# listens on 0.0.0.0\n"),
        ("syscall filter order", lazy("SystemCallFilter=@system-service\nSystemCallFilter=~@privileged @resources", "SystemCallFilter=~@privileged @resources\nSystemCallFilter=@system-service")),
    ],
)
def test_d3_negative_controls_are_caught(label, make):
    assert unit_problems(make()) != [], label


# ------------------------------------------------------------------ pyproject


def backend_versions() -> dict[str, str]:
    data = tomllib.loads(_text(BACKEND_LOCK_PATH))
    return {p["name"]: p["version"] for p in data["package"] if "version" in p}


def test_d2_pyproject_pins():
    raw = _text(PYPROJECT_PATH)
    data = tomllib.loads(raw)
    assert data["project"]["name"] == "growin-gateway-vm"
    assert data["project"]["requires-python"] == ">=3.11,<3.13"
    assert data["project"]["dependencies"] == EXPECTED_DIRECT
    assert data["tool"]["uv"]["package"] is False
    assert data["tool"]["uv"]["constraint-dependencies"] == EXPECTED_CONSTRAINTS
    assert "breeze" not in raw.lower()  # the vendor SDK is never listed (GATE-02)


def test_d2_every_pin_equals_the_backend_lock():
    data = tomllib.loads(_text(PYPROJECT_PATH))
    backend = backend_versions()
    pins = data["project"]["dependencies"] + data["tool"]["uv"]["constraint-dependencies"]
    wrong = []
    for pin in pins:
        name, _, version = pin.partition("==")
        if backend.get(name) != version:
            wrong.append(name)
    assert wrong == []


# ------------------------------------------------------------------ lock (Task 1b)

PYPI = "https://pypi.org/simple"


def _hashes(package: dict) -> set[str]:
    found = {w["hash"] for w in package.get("wheels", [])}
    if "sdist" in package:
        found.add(package["sdist"]["hash"])
    return found


def lock_parity_problems(gateway: dict, backend: dict) -> list[str]:
    problems: list[str] = []
    theirs = {p["name"]: p for p in backend["package"]}
    for package in gateway["package"]:
        name = package["name"]
        source = package.get("source", {})
        if source.get("virtual") == ".":
            continue
        if source.get("registry") != PYPI:
            problems.append(f"{name}: source is not the PyPI registry")
            continue
        other = theirs.get(name)
        if other is None or other["version"] != package["version"]:
            problems.append(f"{name}: not in the backend lock at this version")
            continue
        hashes = _hashes(package)
        if not hashes:  # an empty set is a subset of anything: an entry with no hash proves nothing
            problems.append(f"{name}: no hashes in the gateway lock")
        elif not hashes <= _hashes(other):
            problems.append(f"{name}: hash not in the backend lock")
    return problems


def _synthetic_gateway_lock() -> tuple[dict, dict]:
    backend = tomllib.loads(_text(BACKEND_LOCK_PATH))
    wanted = {p.partition("==")[0] for p in EXPECTED_DIRECT + EXPECTED_CONSTRAINTS}
    picked = [copy.deepcopy(p) for p in backend["package"] if p["name"] in wanted]
    for package in picked:
        # a gateway lock holds a subset of the hashes
        package["wheels"] = package.get("wheels", [])[:1]
        package.pop("sdist", None)
    virtual = {"name": "growin-gateway-vm", "version": "0.1.0", "source": {"virtual": "."}}
    return {"package": [virtual, *picked]}, backend


def test_d1_parity_helper_accepts_a_clean_subset():
    gateway, backend = _synthetic_gateway_lock()
    assert {p["name"] for p in gateway["package"]} >= {"fastapi", "cryptography", "cffi", "pycparser"}
    assert lock_parity_problems(gateway, backend) == []


def test_d1_parity_helper_catches_a_changed_hash():  # P1
    gateway, backend = _synthetic_gateway_lock()
    target = next(p for p in gateway["package"] if p["name"] == "fastapi")
    target["wheels"][0]["hash"] = "sha256:" + "0" * 64
    assert any("fastapi" in p for p in lock_parity_problems(gateway, backend))


def test_d1_parity_helper_catches_a_git_source():  # P2
    gateway, backend = _synthetic_gateway_lock()
    gateway["package"].append({"name": "evil", "version": "1.0", "source": {"git": "https://example.invalid/x"}})
    assert any("evil" in p for p in lock_parity_problems(gateway, backend))


def test_d1_parity_helper_catches_an_entry_with_no_hashes():
    gateway, backend = _synthetic_gateway_lock()
    target = next(p for p in gateway["package"] if p["name"] == "fastapi")
    target["wheels"] = []
    target.pop("sdist", None)
    assert any("fastapi" in p and "no hashes" in p for p in lock_parity_problems(gateway, backend))


def test_d1_parity_helper_catches_a_new_version():
    gateway, backend = _synthetic_gateway_lock()
    next(p for p in gateway["package"] if p["name"] == "h11")["version"] = "0.0.1"
    assert any("h11" in p for p in lock_parity_problems(gateway, backend))


@pytest.mark.skipif(not LOCK_PATH.exists(), reason=NO_LOCK)
def test_d1_lock_parity():
    gateway = tomllib.loads(_text(LOCK_PATH))
    backend = tomllib.loads(_text(BACKEND_LOCK_PATH))
    assert lock_parity_problems(gateway, backend) == []
    assert {p["name"] for p in gateway["package"]} >= {"cryptography", "cffi", "pycparser"}


# ------------------------------------------------- import closure (D7) and data (D8)

STDLIB = set(sys.stdlib_module_names)


def third_party_imports(root: Path, own: str = "gateway_vm") -> list[tuple[str, str]]:
    """(top-level name, 'file:line') for every absolute import that is neither stdlib nor ours."""
    found: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name not in STDLIB and name != own:
                    found.append((name, f"{path.relative_to(root)}:{node.lineno}"))
    return found


def closure_problems(root: Path, locked: set[str]) -> list[str]:
    problems = []
    for name, where in third_party_imports(root):
        dist = IMPORT_TO_DIST.get(name)
        if dist is None:
            problems.append(f"{where}: unknown third-party import {name}")
        elif dist not in locked:
            problems.append(f"{where}: {dist} is not locked")
    return problems


def pyproject_dists() -> set[str]:
    data = tomllib.loads(_text(PYPROJECT_PATH))
    pins = data["project"]["dependencies"] + data["tool"]["uv"]["constraint-dependencies"]
    return {p.partition("==")[0] for p in pins}


def test_d7_every_import_maps_to_a_pinned_package():
    assert closure_problems(VM / "gateway_vm", pyproject_dists()) == []
    seen = {name for name, _ in third_party_imports(VM / "gateway_vm")}
    assert {"fastapi", "cryptography", "uvicorn", "pydantic"} <= seen


def test_d7_plants_are_caught(tmp_path):  # P4
    copy_root = tmp_path / "gateway_vm"
    shutil.copytree(VM / "gateway_vm", copy_root, ignore=shutil.ignore_patterns("__pycache__"))
    (copy_root / "app.py").write_text(
        (copy_root / "app.py").read_text(encoding="utf-8") + "\nimport httpx\n", encoding="utf-8"
    )
    problems = closure_problems(copy_root, pyproject_dists())
    assert any("httpx" in p for p in problems)
    # P3: dropping cryptography from the locked set is caught as well
    assert any("cryptography" in p for p in closure_problems(VM / "gateway_vm", pyproject_dists() - {"cryptography"}))


@pytest.mark.skipif(not LOCK_PATH.exists(), reason=NO_LOCK)
def test_d7_every_import_is_in_the_lock():
    gateway = tomllib.loads(_text(LOCK_PATH))
    assert closure_problems(VM / "gateway_vm", {p["name"] for p in gateway["package"]}) == []


def test_d8_packaged_data_files_are_tracked():
    out = subprocess.run(
        ["git", "ls-files", "gateway/vm"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for name in ("nse_trading_holidays.json", "nse_cash_tick_sizes.json"):
        assert f"gateway/vm/gateway_vm/orders/data/{name}" in out


# ------------------------------------------------------------------ example config

FILLED = {"<EXPECTED_EGRESS_IP>": "203.0.113.7", "<EXPECTED_USERID>": "FAKEUSER01"}


def _private_copy(tmp_path: Path, text: str, name: str = "relay.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_d4_example_is_refused_until_filled(tmp_path):
    with pytest.raises(relay_config.ConfigError) as exc:
        relay_config.load_relay_config(_private_copy(tmp_path, _text(EXAMPLE_PATH)))
    assert exc.value.code == "placeholder"
    text = _text(EXAMPLE_PATH)
    for old, new in FILLED.items():
        text = text.replace(old, new)
    loaded = relay_config.load_relay_config(_private_copy(tmp_path, text, "filled.json"))
    assert (loaded.per_minute, loaded.per_day, loaded.security_master_per_hour) == (80, 4500, 6)
    assert loaded.limits_path == relay_config.DEFAULT_LIMITS_PATH
    assert loaded.order_key_path == relay_config.DEFAULT_ORDER_KEY_PATH
    assert loaded.expected_egress_ip == "203.0.113.7"


def test_d4_one_placeholder_left_is_still_refused(tmp_path):
    text = _text(EXAMPLE_PATH).replace("<EXPECTED_EGRESS_IP>", "203.0.113.7")
    with pytest.raises(relay_config.ConfigError) as exc:
        relay_config.load_relay_config(_private_copy(tmp_path, text))
    assert exc.value.code == "placeholder"


def test_d12_example_keys():
    keys = set(json.loads(_text(EXAMPLE_PATH)))
    assert keys == {
        "expected_egress_ip",
        "expected_userid",
        "secret_names",
        "egress_echo_url",
        "security_master_url",
        "per_minute",
        "per_day",
        "security_master_per_hour",
    }
    assert "limits_path" not in keys and "order_key_path" not in keys


# ------------------------------------------------- namespace simulation (D13)


def _mode(text: str) -> int:
    return int(text, 8)


def _chmod(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def _rel(path: str) -> str:
    return path.lstrip("/")


def write_host(host: Path, modes: dict[str, int], *, order_files: bool) -> None:
    """The host's /etc/growin-gateway with the fixture files at the given modes."""
    etc = host / _rel(ETC_DIR)
    etc.mkdir(parents=True)
    filled = _text(EXAMPLE_PATH)
    for old, new in FILLED.items():
        filled = filled.replace(old, new)
    files = {RELAY_JSON_HOST: filled}
    if order_files:
        files[LIMITS_PATH] = json.dumps(VECTORS["limits"])
        files[ORDER_KEY_PATH] = json.dumps(
            {"key_id": PRIMARY["key_id"], "public_key_x963_hex": PRIMARY["public_key_x963_hex"]}
        )
    for path, body in files.items():
        target = host / _rel(path)
        target.write_text(body, encoding="ascii")
        _chmod(target, modes[path])
    _chmod(etc, modes[ETC_DIR])


class View:
    def __init__(self, ns: Path, entries) -> None:
        self.ns = ns
        self.entries = entries
        self.credentials_dir = ns / "run" / "credentials" / "growin-gateway.service"
        self.state_dir = ns / "var" / "lib" / "growin-gateway"

    def at(self, path: str) -> Path:
        return self.ns / _rel(path)

    def providers(self, path: str) -> list[str]:
        """Unit lines that put `path` into the service's view."""
        found = []
        for _, key, value in (e for e in self.entries if e[0] == "Service"):
            if key == "BindReadOnlyPaths" and value.removeprefix("-") == path:
                found.append(f"{key}={value}")
            if key == "LoadCredential":
                name = value.partition(":")[0]
                if path == f"$CREDENTIALS_DIRECTORY/{name}":
                    found.append(f"{key}={value}")
            if key == "StateDirectory" and path == "$STATE_DIRECTORY":
                found.append(f"{key}={value}")
        return found


def materialize_unit_view(entries, host_root: Path, ns_root: Path) -> View:
    """Build, under ns_root, the files the unit would show its service (see systemd.exec(5))."""
    ns_root.mkdir(parents=True, exist_ok=True)
    _chmod(ns_root, 0o755)
    service = [(k, v) for s, k, v in entries if s == "Service"]
    tmpfs: set[str] = set()
    for key, value in service:
        if key != "TemporaryFileSystem":
            continue
        path, _, options = value.partition(":")
        mode = 0o755
        for option in options.split(","):
            if option.startswith("mode="):
                mode = _mode(option[5:])
        target = ns_root / _rel(path)
        target.mkdir(parents=True, exist_ok=True)
        _chmod(target, mode)
        tmpfs.add(path)
    for key, value in service:
        if key != "BindReadOnlyPaths":
            continue
        optional = value.startswith("-")
        path = value.removeprefix("-")
        source = host_root / _rel(path)
        if not source.exists():
            assert optional, "a mandatory bind source is missing"
            continue  # systemd skips a missing source behind "-"
        parent = str(Path(path).parent)
        target = ns_root / _rel(path)
        if parent not in tmpfs:
            # No tmpfs hides the host directory: the service sees the host directory and its mode.
            target.parent.mkdir(parents=True, exist_ok=True)
            host_dir = host_root / _rel(parent)
            _chmod(target.parent, stat.S_IMODE(host_dir.stat().st_mode))
        shutil.copy2(source, target)  # keeps the mode bits
    for key, value in service:
        if key == "LoadCredential":
            name, _, source_path = value.partition(":")
            view_dir = ns_root / "run" / "credentials" / "growin-gateway.service"
            view_dir.mkdir(parents=True, exist_ok=True)
            _chmod(view_dir, 0o700)
            copy = view_dir / name
            shutil.copy2(host_root / _rel(source_path), copy)
            _chmod(copy, 0o400)
    for key, value in service:
        if key == "StateDirectory":
            private = ns_root / "var" / "lib" / "private" / value
            private.mkdir(parents=True)
            _chmod(private, 0o700)
            (ns_root / "var" / "lib" / value).symlink_to(Path("private") / value)
    return View(ns_root, entries)


class _OrderStack:
    """The same real objects the routes test wires, with no network and no files of its own."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.transport = FakeTransport()
        self.events = EventLog(io.StringIO(), self.clock)
        self.budget = BudgetDouble()
        self.credentials = Credentials(app_key=APP_KEY, secret_key=SECRET_KEY)
        self.breeze = BreezeClient(self.transport, clock=self.clock)
        egress = EgressChecker(
            self.transport, expected_ip=EXPECTED_IP, echo_url="https://api.ipify.org", clock=self.clock
        )
        self.session = SessionManager(
            clock=self.clock,
            egress=egress,
            breeze=self.breeze,
            credentials=self.credentials,
            expected_userid=USERID,
            events=self.events,
            budget=self.budget,
        )

    def build(self, view: View, *, owner_uid: int):
        return build_order_service(
            OrderSettings(
                state_dir=view.state_dir,
                limits_path=str(view.at(LIMITS_PATH)),
                key_path=str(view.at(ORDER_KEY_PATH)),
                owner_uid=owner_uid,
                allow_test_key=True,
                kill_base_url="http://127.0.0.1:9",  # nothing is called while building
                holidays=frozenset(),
                special_sessions=frozenset(),
            ),
            session=self.session,
            breeze=self.breeze,
            credentials=self.credentials,
            budget=self.budget,
            events=self.events,
            clock=self.clock,
            security_master_url=None,
        )


def _reach(view: View, path: str) -> list[str]:
    """Can a user that is neither owner nor group read `path`? Walk every directory down to it."""
    problems = []
    current = view.ns
    for part in Path(_rel(path)).parent.parts:
        current = current / part
        if not current.is_dir():
            problems.append(f"{current.relative_to(view.ns)}: missing")
            return problems
        if not stat.S_IMODE(current.stat().st_mode) & stat.S_IXOTH:
            problems.append(f"{current.relative_to(view.ns)}: no other-execute")
    target = view.at(path)
    if not target.exists():
        problems.append(f"{path}: not in the view")
        return problems
    mode = stat.S_IMODE(target.stat().st_mode)
    if not mode & stat.S_IROTH:
        problems.append(f"{path}: not other-readable")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        problems.append(f"{path}: group or other writable")
    return problems


def simulate(
    unit_text: str,
    tmp_path: Path,
    monkeypatch,
    *,
    modes: dict[str, int] | None = None,
    owner_shift: int = 0,
    credential_modes: tuple[int, ...] = (0o400, 0o440, 0o600),
) -> list[tuple[str, str]]:
    """Return (stage, problem) pairs for the unit's namespace. Stages: resolution, reach, load."""
    problems: list[tuple[str, str]] = []
    host = tmp_path / "host"
    write_host(host, modes or HOST_MODES, order_files=True)
    view = materialize_unit_view(parse_unit(unit_text), host, tmp_path / "ns")

    # (1) resolution: each path the app reads has exactly one providing unit line
    for path in ("$CREDENTIALS_DIRECTORY/relay.json", LIMITS_PATH, ORDER_KEY_PATH, "$STATE_DIRECTORY"):
        if len(view.providers(path)) != 1:
            problems.append(("resolution", f"{path}: {len(view.providers(path))} providers"))

    # (2) reach
    for path in (LIMITS_PATH, ORDER_KEY_PATH):
        problems.extend(("reach", p) for p in _reach(view, path))
    state_mode = stat.S_IMODE(view.state_dir.resolve().stat().st_mode)
    if state_mode != 0o700 or view.state_dir.resolve().stat().st_uid != os.getuid():
        problems.append(("reach", "state directory is not 0700 and owned by the service"))

    # (3) load
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(view.credentials_dir))
    relay_json = view.credentials_dir / "relay.json"
    for mode in credential_modes:
        _chmod(relay_json, mode)
        try:
            relay_config.load_relay_config(relay_json)
        except relay_config.ConfigError as exc:
            problems.append(("load", f"relay.json at {oct(mode)}: {exc.code}"))
    service = _OrderStack().build(view, owner_uid=os.getuid() + owner_shift)
    if service.unavailable is not None:
        problems.append(("load", f"order service unavailable: {service.unavailable.code}"))
    store = FileCounterStore(view.state_dir)
    try:
        from datetime import date

        store.save(date(2026, 11, 5), 3)
        if store.load() != (date(2026, 11, 5), 3):
            problems.append(("load", "counter round trip"))
    except Exception as exc:  # noqa: BLE001 - any failure is a problem row
        problems.append(("load", f"counter store: {type(exc).__name__}"))
    return problems


def test_d13_unit_paths_resolve_and_load(tmp_path, monkeypatch):
    assert simulate(_text(UNIT_PATH), tmp_path, monkeypatch) == []


def test_d13_before_63_06_the_order_routes_are_config_invalid(tmp_path, monkeypatch):
    host = tmp_path / "host"
    write_host(host, HOST_MODES, order_files=False)
    view = materialize_unit_view(parse_unit(_text(UNIT_PATH)), host, tmp_path / "ns")
    assert not view.at(LIMITS_PATH).exists() and not view.at(ORDER_KEY_PATH).exists()
    assert view.at(ETC_DIR).is_dir() and list(view.at(ETC_DIR).iterdir()) == []
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(view.credentials_dir))
    relay_config.load_relay_config(view.credentials_dir / "relay.json")
    service = _OrderStack().build(view, owner_uid=os.getuid())
    assert service.unavailable is not None and service.unavailable.code == "config_invalid"
    assert FileCounterStore(view.state_dir).load() is None  # counter store works, nothing written yet


def test_d13_relay_json_is_not_visible_at_its_etc_path(tmp_path):
    host = tmp_path / "host"
    write_host(host, HOST_MODES, order_files=True)
    view = materialize_unit_view(parse_unit(_text(UNIT_PATH)), host, tmp_path / "ns")
    assert not view.at(RELAY_JSON_HOST).exists()  # only the credential copy exists
    assert (view.credentials_dir / "relay.json").is_file()


def _stages(problems) -> set[str]:
    return {stage for stage, _ in problems}


def test_d13_p14_without_the_tmpfs_the_view_is_unreachable(tmp_path, monkeypatch):
    text = _mutate("TemporaryFileSystem=/etc/growin-gateway:ro,mode=0755\n", "")
    assert "reach" in _stages(simulate(text, tmp_path, monkeypatch))


def test_d13_p15_a_0700_tmpfs_is_unreachable(tmp_path, monkeypatch):
    text = _mutate("ro,mode=0755", "ro,mode=0700")
    assert "reach" in _stages(simulate(text, tmp_path, monkeypatch))


def test_d13_p17_a_bind_of_the_wrong_file_leaves_no_provider(tmp_path, monkeypatch):
    text = _mutate("BindReadOnlyPaths=-/etc/growin-gateway/limits.json", "BindReadOnlyPaths=-/etc/growin-gateway/limits.json.new")
    assert "resolution" in _stages(simulate(text, tmp_path, monkeypatch))


def test_d13_p18_a_0600_limits_file_is_unreadable_to_the_service(tmp_path, monkeypatch):
    modes = {**HOST_MODES, LIMITS_PATH: 0o600}
    assert "reach" in _stages(simulate(_text(UNIT_PATH), tmp_path, monkeypatch, modes=modes))


def test_d13_p18_a_group_writable_limits_file_is_refused_by_the_loader(tmp_path, monkeypatch):
    modes = {**HOST_MODES, LIMITS_PATH: 0o664}
    assert "load" in _stages(simulate(_text(UNIT_PATH), tmp_path, monkeypatch, modes=modes))


def test_d13_p19_the_owner_check_still_runs_on_the_bound_file(tmp_path, monkeypatch):
    assert "load" in _stages(simulate(_text(UNIT_PATH), tmp_path, monkeypatch, owner_shift=1))


# ------------------------------------------------------------------ runbook text

RUNBOOK = _text(RUNBOOK_PATH)
SECTION_RE = re.compile(r"^## (\d+)\. (.*)$", re.MULTILINE)


def section(text: str, number: int) -> str:
    matches = list(SECTION_RE.finditer(text))
    for i, match in enumerate(matches):
        if int(match.group(1)) == number:
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[match.start() : end]
    raise AssertionError(f"RUNBOOK has no section {number}")


def test_d5_runbook_required_text():
    for needle in (
        "growin-gateway.service",
        "login --paste",
        "gcloud auth revoke",
        "application-default revoke",
        "check mac-credentials",
        "scan_secrets.py mac",
        "scan_secrets.py vm",
        "systemd-analyze security",
        "--tunnel-through-iap",
    ):
        assert needle in RUNBOOK, needle
    for banned in ("gateway_vm.paste_login", "127.0.0.1:8443"):
        assert banned not in RUNBOOK, banned


def test_d5_every_placeholder_is_upper_snake_case():
    bad = [m for m in re.findall(r"<[^<>\s]+>", RUNBOOK) if not re.fullmatch(r"<[A-Z0-9_]+>", m)]
    assert bad == []


def test_d5_no_ip_address_or_key_shaped_text_in_the_runbook():
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", RUNBOOK)
    assert not re.search(r"\b[0-9a-f]{64}\b", RUNBOOK)


def test_d9_smoke_names_every_route_and_stage():
    smoke = section(RUNBOOK, 9)
    for needle in ("orders-key", "orders-state", "orders-audit", "probe-daily", "NO_SESSION", "config_invalid", "kill blocked"):
        assert needle in smoke, needle


def test_d18_smoke_has_the_intraday_step():
    smoke = section(RUNBOOK, 9)
    for needle in ("probe-intraday step --step S-3", "mktemp -d", "INCONCLUSIVE", "7 charged calls"):
        assert needle in smoke, needle


def test_d10_login_gate():
    heading = re.search(r"^## \d+\. Only if 61-10 lands$", RUNBOOK, re.MULTILINE)
    assert heading, "the daemon text needs its own 'Only if 61-10 lands' heading"
    gated = section(RUNBOOK, int(heading.group(0).split(".")[0].split()[-1]))
    outside = RUNBOOK.replace(gated, "")
    installed = (ROOT / "gateway" / "mac" / "INSTALL.md").exists()
    if installed:
        assert "daemon-check" in gated and "INSTALL.md" in gated
    else:
        assert "login --paste" in outside
        for token in ("daemon-check", "INSTALL.md"):
            assert token not in outside, f"{token} outside the 61-10 heading while gateway/mac is absent"
        assert "daemon-check" in gated  # the gated text exists, so landing 61-10 needs no rewrite


def test_d10_p11_a_bare_daemon_check_outside_the_heading_is_caught():
    heading = re.search(r"^## \d+\. Only if 61-10 lands$", RUNBOOK, re.MULTILINE)
    planted = RUNBOOK.replace("growin_cli login --paste\ngrowin_cli health", "growin_cli daemon-check\ngrowin_cli health", 1)
    assert planted != RUNBOOK and heading
    gated = section(planted, int(heading.group(0).split(".")[0].split()[-1]))
    assert "daemon-check" in planted.replace(gated, "")


def test_d14_file_contract_text_matches_the_host_mode_table():
    five = section(RUNBOOK, 5)
    found = {
        m.group(3): int(m.group(2), 8)
        for m in re.finditer(r"install (?:-d )?-o root -g root -m (0?)(\d{3,4}) (?:\S+ )?(/etc/growin-gateway\S*)", five)
    }
    assert found == HOST_MODES
    six = section(RUNBOOK, 6)
    assert "nsenter -t" in six and "ls -la /etc/growin-gateway" in six
    assert f"stat -c '%u %a %F %n' {LIMITS_PATH} {ORDER_KEY_PATH}" in six
    assert "0 644 regular file" in six


# --------------------------------------------------- smoke order rule (D22)

RESTART_RE = re.compile(r"restart", re.IGNORECASE)
LOGIN_RE = re.compile(r"login --paste")
ORDER_READ_RE = re.compile(r"orders-(state|audit|key)")


def smoke_order_violations(smoke: str) -> list[str]:
    """Every restart must be followed by a `login --paste` before the next order read that is not
    a NO_SESSION line. Checked at every interval, not only the first."""
    events: list[tuple[int, str, str]] = []
    for number, line in enumerate(smoke.splitlines(), start=1):
        base = sum(len(x) + 1 for x in smoke.splitlines()[: number - 1])
        for match in RESTART_RE.finditer(line):
            events.append((base + match.start(), "restart", line))
        for match in LOGIN_RE.finditer(line):
            events.append((base + match.start(), "login", line))
        for match in ORDER_READ_RE.finditer(line):
            if "NO_SESSION" not in line:
                events.append((base + match.start(), "read", line))
    events.sort(key=lambda e: e[0])
    violations = []
    needs_login = False
    for _, kind, line in events:
        if kind == "restart":
            needs_login = True
        elif kind == "login":
            needs_login = False
        elif needs_login:
            violations.append(line.strip())
    return violations


def test_d22_smoke_logs_in_after_every_restart_before_an_order_read():
    assert smoke_order_violations(section(RUNBOOK, 9)) == []


def test_d22_a_second_restart_with_no_login_is_caught():
    smoke = section(RUNBOOK, 9) + "\nThen run systemctl restart growin-gateway.\n\ngrowin_cli orders-state\n"
    assert smoke_order_violations(smoke) != []


def test_d22_p29_an_order_read_above_the_post_restart_login_is_caught():
    smoke = section(RUNBOOK, 9)
    planted = smoke.replace(
        "After 63-06 Task 3 has installed the files and restarted the unit, log in again first, then read:\n\n```bash\ngrowin_cli login --paste\ngrowin_cli orders-state",
        "After 63-06 Task 3 has installed the files and restarted the unit, read first:\n\n```bash\ngrowin_cli orders-state\ngrowin_cli login --paste",
    )
    assert planted != smoke
    assert smoke_order_violations(planted) != []


def scan_before_revoke_problem(smoke: str) -> str | None:
    """The Mac scan needs the token file that `revoke` deletes, so it must come first in the smoke."""
    scan = smoke.find("scan_secrets.py mac")
    revoke = smoke.find("growin_cli revoke")
    if scan < 0:
        return "section 9 has no Mac scan"
    if revoke < 0:
        return "section 9 has no revoke"
    return None if scan < revoke else "the Mac scan comes after revoke"


def test_d22_the_mac_scan_runs_before_revoke_in_the_smoke():
    assert scan_before_revoke_problem(section(RUNBOOK, 9)) is None
    assert "scan_secrets.py mac" not in section(RUNBOOK, 10)  # one place, so the order cannot disagree


def test_d22_a_mac_scan_after_revoke_is_caught():  # negative control
    smoke = section(RUNBOOK, 9)
    block_start = smoke.index("Mac value scan, now")
    block_end = smoke.index("Revoke:")
    block = smoke[block_start:block_end]
    moved = smoke[:block_start] + smoke[block_end:] + "\n" + block
    assert scan_before_revoke_problem(moved) == "the Mac scan comes after revoke"
    assert scan_before_revoke_problem(smoke.replace("scan_secrets.py mac", "scan_secrets.py")) is not None
