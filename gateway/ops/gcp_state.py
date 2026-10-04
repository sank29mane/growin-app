#!/usr/bin/env python3
"""Read-only GCP state checker for the gateway VM (GATE-01, GATE-03, OD-8, OD-16).

Every check prints one line: "PASS name", "WARN name: reason" or "FAIL name:
reason". Reasons are fixed phrases. No project id, e-mail address, IP address or
instance name is ever printed. The process exits 1 when any check fails.

Nothing here mutates GCP. Reads go through an injected Runner (the real one runs
gcloud). The admin-window remote checks go over IAP: gcloud compute ssh ... --tunnel-through-iap
The daily-access check makes three attempts as the daily identity that are
expected to fail or to stop at the IAP backend.

Usage:
  gcp_state.py merge-audit-config IN -o OUT
  gcp_state.py check pre-window|post-window|external|mac-credentials|daily-access
               --config PATH [--expect-relay] [--daily-roles-ok]

Stdlib only.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:  # run as a script from gateway/ops, or imported by tests with that dir on sys.path
    from verify_firewall import evaluate as evaluate_firewall
except ImportError:  # pragma: no cover - only when the directory is not importable
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify_firewall import evaluate as evaluate_firewall

Runner = Callable[[Sequence[str]], str]
Prober = Callable[[Sequence[str], float], tuple[int, str]]

SECRETS = ("breeze-api-key", "breeze-api-secret")
REQUIRED_APIS = ("secretmanager.googleapis.com", "oslogin.googleapis.com", "iap.googleapis.com")
ROLE_ACCESSOR = "roles/secretmanager.secretAccessor"
ROLE_TUNNEL = "roles/iap.tunnelResourceAccessor"
ROLE_OSADMIN = "roles/compute.osAdminLogin"
ROLE_SA_USER = "roles/iam.serviceAccountUser"
SCOPE_CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
ADC_PATH = Path("~/.config/gcloud/application_default_credentials.json")
SSH_PORT = 22
PROBE_TIMEOUT_S = 25.0
_PLACEHOLDER = re.compile(r"<[^>]*>")
# Listeners expected on the VM before the relay exists (systemd-resolved on loopback, sshd).
BASE_LISTENERS = frozenset({"0.0.0.0:22", "[::]:22", "127.0.0.53:53", "127.0.0.54:53"})


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | FAIL | WARN
    reason: str = ""


@dataclass(frozen=True)
class GcpConfig:
    project: str
    zone: str
    instance: str
    daily_account: str
    relay_port: int = 8443

    @property
    def region(self) -> str:
        return self.zone.rsplit("-", 1)[0]

    @property
    def sa_email(self) -> str:
        return f"breeze-gateway@{self.project}.iam.gserviceaccount.com"


def _ok(name: str, reason: str = "") -> CheckResult:
    return CheckResult(name, "PASS", reason)


def _bad(name: str, reason: str) -> CheckResult:
    return CheckResult(name, "FAIL", reason)


def _check(name: str, passed: bool, reason: str) -> CheckResult:
    return _ok(name) if passed else _bad(name, reason)


# ------------------------------------------------------------------ config


def load_config(path: str | os.PathLike[str]) -> GcpConfig:
    """Load the gcp and tunnel sections. Refuses placeholders and option-like values."""
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    gcp = data["gcp"]
    values = {}
    for key in ("project", "zone", "instance", "daily_account"):
        value = gcp[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("config value missing")
        if _PLACEHOLDER.search(value) or value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("config value is a placeholder or not valid")
        values[key] = value
    relay_port = 8443
    tunnel = data.get("tunnel")
    if isinstance(tunnel, dict) and "relay_port" in tunnel:
        relay_port = tunnel["relay_port"]
    if isinstance(relay_port, bool) or not isinstance(relay_port, int) or not 1 <= relay_port <= 65535:
        raise ValueError("relay_port invalid")
    return GcpConfig(relay_port=relay_port, **values)


# ----------------------------------------------------------------- runners


def real_runner(argv: Sequence[str]) -> str:
    if not argv or argv[0] != "gcloud":
        raise ValueError("only gcloud may be run")
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=True, timeout=120)
    return proc.stdout


def real_prober(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run a command, terminate it at the timeout, return (exit code, stdout+stderr)."""
    if not argv or argv[0] != "gcloud":
        raise ValueError("only gcloud may be run")
    proc = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    return proc.returncode, out or ""


def real_adc_exists() -> bool:
    return ADC_PATH.expanduser().exists()


def real_connect(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _json(run: Runner, args: Sequence[str]):
    """Run a gcloud read with --format=json. Returns the parsed value or None."""
    try:
        return json.loads(run(["gcloud", *args, "--format=json"]))
    except Exception:
        return None


def _text(run: Runner, args: Sequence[str]) -> str | None:
    try:
        return run(["gcloud", *args]).strip()
    except Exception:
        return None


# ------------------------------------------------------ audit-config merge


def merge_secretmanager_data_read(policy: dict) -> tuple[dict, bool]:
    """Add DATA_READ audit logging for Secret Manager, preserving everything else."""
    if not isinstance(policy, dict) or not policy.get("etag"):
        raise ValueError("policy has no etag")
    merged = copy.deepcopy(policy)
    service = "secretmanager.googleapis.com"
    configs = merged.get("auditConfigs")
    if configs is None:
        configs = merged["auditConfigs"] = []
    entry = next((c for c in configs if isinstance(c, dict) and c.get("service") == service), None)
    if entry is None:
        configs.append({"service": service, "auditLogConfigs": [{"logType": "DATA_READ"}]})
        return merged, True
    logs = entry.setdefault("auditLogConfigs", [])
    if any(isinstance(item, dict) and item.get("logType") == "DATA_READ" for item in logs):
        return merged, False
    logs.append({"logType": "DATA_READ"})
    return merged, True


def _write_private(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


# ------------------------------------------------------------- policy helpers


def _bindings(policy: object) -> list[dict]:
    if not isinstance(policy, dict):
        return []
    return [b for b in policy.get("bindings", []) if isinstance(b, dict)]


def _has_member(policy: object, role: str, member: str) -> bool:
    return any(
        b.get("role") == role and member.lower() in [str(m).lower() for m in b.get("members", [])]
        for b in _bindings(policy)
    )


def _user(email: str) -> str:
    return f"user:{email}"


def _meta_items(instance: dict) -> dict[str, str]:
    items = (instance.get("metadata") or {}).get("items") or []
    return {i.get("key"): str(i.get("value", "")) for i in items if isinstance(i, dict)}


def _path(url: str) -> str:
    index = url.find("/projects/")
    return url[index:] if index >= 0 else url


def _nat_ip(instance: dict) -> str | None:
    try:
        return instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
    except (KeyError, IndexError, TypeError):
        return None


def _internal_ip(instance: dict) -> str | None:
    try:
        return instance["networkInterfaces"][0]["networkIP"]
    except (KeyError, IndexError, TypeError):
        return None


def _describe_instance(cfg: GcpConfig, run: Runner):
    return _json(
        run,
        ["compute", "instances", "describe", cfg.instance, f"--zone={cfg.zone}",
         f"--project={cfg.project}"],
    )


# --------------------------------------------------------------- pre-window


def check_pre_window(cfg: GcpConfig, run: Runner) -> list[CheckResult]:
    results: list[CheckResult] = []
    project = f"--project={cfg.project}"
    sa = cfg.sa_email
    read_failed = "read failed"

    apis = _json(run, ["services", "list", "--enabled", project])
    if apis is None:
        results.append(_bad("apis_enabled", read_failed))
    else:
        enabled = {a.get("config", {}).get("name") for a in apis if isinstance(a, dict)}
        results.append(_check("apis_enabled", all(a in enabled for a in REQUIRED_APIS),
                              "required api not enabled"))

    described = _json(run, ["iam", "service-accounts", "describe", sa, project])
    results.append(
        _bad("sa_exists", "service account missing") if not isinstance(described, dict)
        else _check("sa_exists", not described.get("disabled", False), "service account disabled")
    )

    keys = _json(run, ["iam", "service-accounts", "keys", "list", f"--iam-account={sa}",
                       "--managed-by=user", project])
    results.append(
        _bad("sa_no_user_keys", read_failed) if keys is None
        else _check("sa_no_user_keys", keys == [], "service account has a user-managed key")
    )

    operator = _text(run, ["config", "get-value", "account"])
    secret_policies: dict[str, object] = {}
    for secret in SECRETS:
        versions = _json(run, ["secrets", "versions", "list", secret, project])
        results.append(
            _bad(f"secret_{secret}_version", read_failed) if versions is None
            else _check(
                f"secret_{secret}_version",
                any(isinstance(v, dict) and str(v.get("state", "")).upper() == "ENABLED"
                    for v in versions),
                "no enabled secret version",
            )
        )
    for secret in SECRETS:
        policy = _json(run, ["secrets", "get-iam-policy", secret, project])
        secret_policies[secret] = policy
        if policy is None:
            results.append(_bad(f"secret_{secret}_single_accessor", read_failed))
            continue
        bindings = _bindings(policy)
        single = (
            len(bindings) == 1
            and bindings[0].get("role") == ROLE_ACCESSOR
            and [str(m) for m in bindings[0].get("members", [])] == [f"serviceAccount:{sa}"]
            and "condition" not in bindings[0]
        )
        reason = "service account not bound" if not bindings else "secret has another binding"
        results.append(_check(f"secret_{secret}_single_accessor", single, reason))

    project_policy = _json(run, ["projects", "get-iam-policy", cfg.project])
    if project_policy is None:
        for name in ("sa_no_project_roles", "audit_data_read", "operator_tunnel_role",
                     "operator_oslogin_role"):
            results.append(_bad(name, read_failed))
    else:
        results.append(_check(
            "sa_no_project_roles",
            not any(f"serviceAccount:{sa}" in b.get("members", []) for b in _bindings(project_policy)),
            "service account holds a project role",
        ))
        audit_ok = any(
            isinstance(c, dict) and c.get("service") == "secretmanager.googleapis.com"
            and any(isinstance(item, dict) and item.get("logType") == "DATA_READ"
                    for item in c.get("auditLogConfigs", []))
            for c in project_policy.get("auditConfigs", [])
        )
        results.append(_check("audit_data_read", audit_ok, "data read audit logging is off"))

        if not operator:
            results.append(_bad("operator_tunnel_role", read_failed))
            results.append(_bad("operator_oslogin_role", read_failed))
        else:
            results.append(_check("operator_tunnel_role",
                                  _has_member(project_policy, ROLE_TUNNEL, _user(operator)),
                                  "operator lacks the iap tunnel role"))
            results.append(_check("operator_oslogin_role",
                                  _has_member(project_policy, ROLE_OSADMIN, _user(operator)),
                                  "operator lacks the os admin login role"))

    sa_policy_new = _json(run, ["iam", "service-accounts", "get-iam-policy", sa, project])
    results.append(
        _bad("operator_sa_user_new", read_failed) if sa_policy_new is None or not operator
        else _check("operator_sa_user_new",
                    _has_member(sa_policy_new, ROLE_SA_USER, _user(operator)),
                    "operator lacks service account user on the new account")
    )

    instance = _describe_instance(cfg, run)
    current_sa = None
    if isinstance(instance, dict):
        accounts = instance.get("serviceAccounts") or []
        if accounts and isinstance(accounts[0], dict):
            current_sa = accounts[0].get("email")
    if instance is None or not operator:
        results.append(_bad("operator_sa_user_current", read_failed))
        current_policy = None
    elif not current_sa:
        results.append(_ok("operator_sa_user_current", "no service account attached"))
        current_policy = None
    else:
        current_policy = _json(run, ["iam", "service-accounts", "get-iam-policy", current_sa, project])
        results.append(
            _bad("operator_sa_user_current", read_failed) if current_policy is None
            else _check("operator_sa_user_current",
                        _has_member(current_policy, ROLE_SA_USER, _user(operator)),
                        "operator lacks service account user on the current account")
        )

    # The daily identity: one conditioned iap role, nothing else (OD-16).
    daily = _user(cfg.daily_account).lower()
    if project_policy is None:
        results.append(_bad("daily_tunnel_role_conditioned", read_failed))
        results.append(_bad("daily_no_other_roles", read_failed))
        return results

    expected_expr = f"destination.port == {cfg.relay_port}"
    tunnel_bindings = [
        b for b in _bindings(project_policy)
        if b.get("role") == ROLE_TUNNEL and daily in [str(m).lower() for m in b.get("members", [])]
    ]
    if not tunnel_bindings:
        results.append(_bad("daily_tunnel_role_conditioned", "daily tunnel role missing"))
    else:
        conditions = [b.get("condition") for b in tunnel_bindings]
        if any(not c for c in conditions):
            results.append(_bad("daily_tunnel_role_conditioned", "daily tunnel role has no condition"))
        elif any(str(c.get("expression", "")).strip() != expected_expr for c in conditions):
            results.append(_bad("daily_tunnel_role_conditioned", "daily tunnel condition differs"))
        else:
            results.append(_ok("daily_tunnel_role_conditioned"))

    other_project = any(
        daily in [str(m).lower() for m in b.get("members", [])] and b not in tunnel_bindings
        for b in _bindings(project_policy)
    )
    extra_policies = [sa_policy_new, current_policy] + list(secret_policies.values())
    other_resource = any(
        daily in [str(m).lower() for b in _bindings(p) for m in b.get("members", [])]
        for p in extra_policies if p is not None
    )
    unreadable = (sa_policy_new is None) or any(v is None for v in secret_policies.values()) or (
        current_sa is not None and current_policy is None
    )
    if other_project or other_resource:
        results.append(_bad("daily_no_other_roles", "daily identity holds more than the conditioned iap role"))
    elif unreadable:
        results.append(_bad("daily_no_other_roles", read_failed))
    else:
        results.append(_ok("daily_no_other_roles"))
    return results


# -------------------------------------------------------------- post-window


def _remote(cfg: GcpConfig, run: Runner, command: str) -> str | None:
    """Admin window only: a read-only command on the VM through IAP."""
    argv = ["gcloud", "compute", "ssh", cfg.instance, f"--zone={cfg.zone}",
            f"--project={cfg.project}", "--tunnel-through-iap", f"--command={command}"]
    try:
        return run(argv)
    except Exception:
        return None


def _parse_listeners(ss_output: str) -> set[str]:
    found = set()
    for line in ss_output.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            found.add(re.sub(r"%[^:\]]+", "", cols[3]))
    return found


def check_post_window(cfg: GcpConfig, run: Runner, *, expect_relay: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    read_failed = "read failed"
    instance = _describe_instance(cfg, run)
    names = ("instance_sa", "instance_scope_cloud_platform", "secure_boot", "os_login_enabled",
             "no_instance_ssh_keys")
    if not isinstance(instance, dict):
        results.extend(_bad(n, read_failed) for n in names)
        results.append(_bad("project_ssh_keys_retired", read_failed))
        results.append(_bad("address_in_use", read_failed))
        nat_ip = internal_ip = None
        meta: dict[str, str] = {}
    else:
        meta = _meta_items(instance)
        accounts = instance.get("serviceAccounts") or []
        account = accounts[0] if accounts and isinstance(accounts[0], dict) else {}
        results.append(_check("instance_sa", account.get("email") == cfg.sa_email,
                              "instance uses another service account"))
        results.append(_check("instance_scope_cloud_platform",
                              SCOPE_CLOUD_PLATFORM in (account.get("scopes") or []),
                              "cloud-platform scope missing"))
        shield = instance.get("shieldedInstanceConfig") or {}
        results.append(_check("secure_boot", shield.get("enableSecureBoot") is True,
                              "secure boot is off"))
        results.append(_check("os_login_enabled", meta.get("enable-oslogin", "").upper() == "TRUE",
                              "os login is not enabled"))
        results.append(_check("no_instance_ssh_keys",
                              "ssh-keys" not in meta and "sshKeys" not in meta,
                              "instance still holds ssh keys"))
        project_info = _json(run, ["compute", "project-info", "describe", f"--project={cfg.project}"])
        if project_info is None:
            results.append(_bad("project_ssh_keys_retired", read_failed))
        else:
            common = {i.get("key") for i in (project_info.get("commonInstanceMetadata") or {}).get("items") or []
                      if isinstance(i, dict)}
            blocked = meta.get("block-project-ssh-keys", "").upper() == "TRUE"
            results.append(_check("project_ssh_keys_retired", "ssh-keys" not in common or blocked,
                                  "project ssh keys still apply"))
        nat_ip = _nat_ip(instance)
        internal_ip = _internal_ip(instance)
        addresses = _json(run, ["compute", "addresses", "list", f"--project={cfg.project}"])
        if addresses is None or not nat_ip:
            results.append(_bad("address_in_use", read_failed))
        else:
            self_path = _path(str(instance.get("selfLink", "")))
            match = [a for a in addresses if isinstance(a, dict) and a.get("address") == nat_ip]
            attached = bool(match) and match[0].get("status") == "IN_USE" and any(
                _path(str(u)) == self_path for u in match[0].get("users", [])
            )
            results.append(_check("address_in_use", attached, "reserved address is not in use by the vm"))

    rules = _json(run, ["compute", "firewall-rules", "list", f"--project={cfg.project}"])
    results.append(
        _bad("icmp_rule_absent", read_failed) if rules is None
        else _check("icmp_rule_absent",
                    not any(isinstance(r, dict) and r.get("name") == "default-allow-icmp" for r in rules),
                    "public icmp rule still present")
    )

    effective = _json(run, ["compute", "instances", "network-interfaces", "get-effective-firewalls",
                            cfg.instance, f"--zone={cfg.zone}", f"--project={cfg.project}"])
    if effective is None:
        results.append(_bad("effective_firewall", read_failed))
    else:
        findings = evaluate_firewall(effective, allow_icmp=False, relay_port=cfg.relay_port)
        if any(f.level == "FAIL" for f in findings):
            results.append(_bad("effective_firewall", "effective firewall has failing rules"))
        elif findings:
            results.append(CheckResult("effective_firewall", "WARN", "effective firewall has warnings"))
        else:
            results.append(_ok("effective_firewall"))

    sb = _remote(cfg, run, "mokutil --sb-state")
    results.append(_check("remote_secure_boot", bool(sb) and "SecureBoot enabled" in sb,
                          "secure boot not enabled on the vm"))

    egress = _remote(
        cfg, run,
        "curl -s -m 5 https://api.ipify.org; echo; curl -s -m 3 -H 'Metadata-Flavor: Google' "
        "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/"
        "access-configs/0/external-ip",
    )
    lines = [ln.strip() for ln in (egress or "").splitlines() if ln.strip()]
    results.append(_check("remote_egress_matches",
                          len(lines) == 2 and lines[0] == lines[1] and bool(nat_ip) and lines[0] == nat_ip,
                          "egress address does not match"))

    listeners_raw = _remote(cfg, run, "ss -ltnH")
    if listeners_raw is None:
        results.append(_bad("remote_listeners", read_failed))
    else:
        found = _parse_listeners(listeners_raw)
        allowed = set(BASE_LISTENERS)
        relay_listener = f"{internal_ip}:{cfg.relay_port}" if internal_ip else None
        if expect_relay and relay_listener:
            allowed.add(relay_listener)
        if found - allowed:
            results.append(_bad("remote_listeners", "unexpected listener"))
        elif expect_relay and (not relay_listener or relay_listener not in found):
            results.append(_bad("remote_listeners", "relay listener missing"))
        else:
            results.append(_ok("remote_listeners"))

    uv = _remote(cfg, run, "test -x /usr/local/bin/uv && echo uv-installed")
    results.append(_check("remote_uv_installed", bool(uv) and "uv-installed" in uv,
                          "uv is not installed system-wide"))
    return results


# ----------------------------------------------------------------- external


def check_external(cfg: GcpConfig, run: Runner, connect: Callable[[str, int, float], bool]) -> list[CheckResult]:
    instance = _describe_instance(cfg, run)
    nat_ip = _nat_ip(instance) if isinstance(instance, dict) else None
    ports = (22, 443, cfg.relay_port)
    if not nat_ip:
        return [_bad(f"external_port_{p}_closed", "no external address found") for p in ports]
    return [
        _check(f"external_port_{port}_closed", not connect(nat_ip, port, 3.0),
               "port reachable from the internet")
        for port in ports
    ]


# ---------------------------------------------------------- mac credentials


def check_mac_credentials(run: Runner, adc_exists: Callable[[], bool], cfg: GcpConfig) -> list[CheckResult]:
    """Run after the admin window closes. Local gcloud state only, no GCP API call."""
    results: list[CheckResult] = []
    active = _text(run, ["config", "get-value", "account"])
    results.append(
        _bad("active_account_is_daily", "read failed") if active is None
        else _check("active_account_is_daily", active.lower() == cfg.daily_account.lower(),
                    "active account is not the daily identity")
    )
    accounts = _json(run, ["auth", "list"])
    if not isinstance(accounts, list):
        results.append(_bad("no_other_gcloud_accounts", "read failed"))
    elif len(accounts) > 1:
        results.append(_bad("no_other_gcloud_accounts", "admin credentials still on this Mac"))
    elif len(accounts) == 0:
        results.append(_bad("no_other_gcloud_accounts", "no credentialed account"))
    else:
        results.append(_ok("no_other_gcloud_accounts"))
    results.append(_check("no_adc_file", not adc_exists(),
                          "application-default credentials file present"))
    return results


# ------------------------------------------------------------- daily access


def _has_listening(output: str) -> bool:
    return "listening on port" in output.lower()


def _iam_denied(output: str) -> bool:
    low = output.lower()
    return "permission" in low or "denied" in low or "4033" in low


def _backend_only(output: str) -> bool:
    low = output.lower()
    return "4003" in low or "failed to connect to backend" in low


def check_daily_access(
    cfg: GcpConfig, probe: Prober, *, daily_roles_ok: bool | None = None
) -> list[CheckResult]:
    """Live attempts as the daily identity. None may mutate anything.

    daily_roles_ok is the recorded result of daily_no_other_roles from the admin
    window; the actAs measurement is only meaningful when it holds.
    """
    account = f"--account={cfg.daily_account}"
    scope = [f"--zone={cfg.zone}", f"--project={cfg.project}"]

    ssh_cmd = ["gcloud", "compute", "ssh", cfg.instance, *scope, "--tunnel-through-iap",
               "--command=true", "--quiet", account]
    code, _out = probe(ssh_cmd, PROBE_TIMEOUT_S)
    results = [_check("daily_cannot_ssh", code != 0, "daily identity can open a shell")]

    def tunnel(port: int) -> list[str]:
        return ["gcloud", "compute", "start-iap-tunnel", cfg.instance, str(port),
                f"--local-host-port=127.0.0.1:{pick_free_port()}", *scope, account]

    # negative-probe: the daily identity must NOT be able to reach the ssh port.
    _code, out22 = probe(tunnel(SSH_PORT), PROBE_TIMEOUT_S)
    results.append(_check("daily_cannot_tunnel_ssh_port", not _has_listening(out22),
                          "daily identity can tunnel to the ssh port"))

    _code, out_relay = probe(tunnel(cfg.relay_port), PROBE_TIMEOUT_S)
    if _iam_denied(out_relay):
        relay = _bad("daily_relay_port_iam", "tunnel denied by IAM")
    elif _has_listening(out_relay) or _backend_only(out_relay):
        relay = _ok("daily_relay_port_iam")
    else:
        relay = _bad("daily_relay_port_iam", "tunnel probe inconclusive")
    results.append(relay)

    if relay.status == "PASS" and daily_roles_ok is True:
        results.append(_ok("actas_not_required", "tunnel path does not need actAs"))
    elif relay.status == "PASS" and daily_roles_ok is None:
        results.append(_bad("actas_not_required", "daily_no_other_roles not confirmed"))
    else:
        results.append(_bad("actas_not_required", "daily tunnel needs more than the conditioned iap role"))
    return results


# --------------------------------------------------------------------- CLI


def render(results: Sequence[CheckResult]) -> list[str]:
    lines = []
    for r in results:
        if r.status == "PASS":
            lines.append(f"PASS {r.name}")
        else:
            lines.append(f"{r.status} {r.name}: {r.reason}")
    return lines


def _emit(results: Sequence[CheckResult]) -> int:
    for line in render(results):
        print(line)
    return 1 if any(r.status == "FAIL" for r in results) else 0


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    prober: Prober | None = None,
    connect: Callable[[str, int, float], bool] | None = None,
    adc_exists: Callable[[], bool] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Read-only GCP state checker.")
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge-audit-config", help="add DATA_READ for Secret Manager")
    merge.add_argument("input")
    merge.add_argument("-o", "--output", required=True)
    check = sub.add_parser("check", help="run a read-only check group")
    check.add_argument(
        "group",
        choices=["pre-window", "post-window", "external", "mac-credentials", "daily-access"],
    )
    check.add_argument("--config", required=True)
    check.add_argument("--expect-relay", action="store_true")
    check.add_argument(
        "--daily-roles-ok", action="store_true",
        help="daily_no_other_roles passed in the admin window (needed for actas_not_required)",
    )
    args = parser.parse_args(argv)

    if args.command == "merge-audit-config":
        try:
            policy = json.loads(Path(args.input).read_text(encoding="utf-8"))
            merged, changed = merge_secretmanager_data_read(policy)
            _write_private(Path(args.output), merged)
        except (OSError, ValueError) as exc:
            reason = "policy has no etag" if "etag" in str(exc) else "cannot read or write policy"
            print(f"FAIL merge-audit-config: {reason}")
            return 1
        print("PASS audit_config_merged" if changed else "PASS audit_config_unchanged")
        return 0

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, KeyError, TypeError):
        print("FAIL config: missing, placeholder or invalid values")
        return 1
    run = runner or real_runner
    if args.group == "pre-window":
        return _emit(check_pre_window(cfg, run))
    if args.group == "post-window":
        return _emit(check_post_window(cfg, run, expect_relay=args.expect_relay))
    if args.group == "external":
        return _emit(check_external(cfg, run, connect or real_connect))
    if args.group == "mac-credentials":
        return _emit(check_mac_credentials(run, adc_exists or real_adc_exists, cfg))
    return _emit(check_daily_access(cfg, prober or real_prober,
                                    daily_roles_ok=True if args.daily_roles_ok else None))


if __name__ == "__main__":
    raise SystemExit(main())
