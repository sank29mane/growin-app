"""gcp_state: offline checks against a fake gcloud runner. No real gcloud is ever spawned.

All account values below are invented (example.invalid, documentation IPs).
"""

from __future__ import annotations

import copy
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "ops") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "ops"))

import gcp_state as gs  # noqa: E402

PROJECT = "proj-example"
ZONE = "asia-south1-a"
VM = "vm-example"
ADMIN = "admin@example.invalid"
DAILY = "daily@example.invalid"
NAT = "203.0.113.7"
INTERNAL = "10.0.0.5"
SA = f"breeze-gateway@{PROJECT}.iam.gserviceaccount.com"
OLD_SA = "111-compute@developer.gserviceaccount.com"
SELF_LINK = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances/{VM}"
VALUES = [PROJECT, VM, ADMIN, DAILY, NAT, INTERNAL, "breeze-gateway@"]

CFG = gs.GcpConfig(project=PROJECT, zone=ZONE, instance=VM, daily_account=DAILY)
IAP = "35.235.240.0/20"


@pytest.fixture(autouse=True)
def no_real_processes(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a test tried to spawn a real process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


# ----------------------------------------------------------------- the world


def secret_policy():
    return {"bindings": [{"role": gs.ROLE_ACCESSOR, "members": [f"serviceAccount:{SA}"]}]}


def make_world(post: bool = False) -> dict:
    instance = {
        "selfLink": SELF_LINK,
        "serviceAccounts": [{
            "email": SA if post else OLD_SA,
            "scopes": [gs.SCOPE_CLOUD_PLATFORM] if post else ["https://www.googleapis.com/auth/devstorage.read_only"],
        }],
        "shieldedInstanceConfig": {"enableSecureBoot": post},
        "metadata": {"items": [{"key": "enable-oslogin", "value": "TRUE"}] if post else []},
        "networkInterfaces": [{"networkIP": INTERNAL, "accessConfigs": [{"natIP": NAT}]}],
    }
    return {
        "operator": ADMIN,
        "apis": [{"config": {"name": n}} for n in (*gs.REQUIRED_APIS, "compute.googleapis.com")],
        "sa": {"email": SA, "disabled": False},
        "keys": [],
        "versions": [{"state": "ENABLED"}],
        "secret_policy": {s: secret_policy() for s in gs.SECRETS},
        "project_policy": {
            "version": 3,
            "etag": "BwX1",
            "bindings": [
                {"role": gs.ROLE_TUNNEL, "members": [f"user:{ADMIN}"]},
                {"role": gs.ROLE_OSADMIN, "members": [f"user:{ADMIN}"]},
                {"role": gs.ROLE_TUNNEL, "members": [f"user:{DAILY}"],
                 "condition": {"title": "relay-port-only",
                               "expression": "destination.port == 8443"}},
                {"role": "roles/owner", "members": [f"user:{ADMIN}"]},
            ],
            "auditConfigs": [{"service": "secretmanager.googleapis.com",
                              "auditLogConfigs": [{"logType": "DATA_READ"}]}],
        },
        "sa_policy": {
            SA: {"bindings": [{"role": gs.ROLE_SA_USER, "members": [f"user:{ADMIN}"]}]},
            OLD_SA: {"bindings": [{"role": gs.ROLE_SA_USER, "members": [f"user:{ADMIN}"]}]},
        },
        "instance": instance,
        "project_info": {"commonInstanceMetadata": {"items": []}},
        "addresses": [{"address": NAT, "status": "IN_USE", "users": [SELF_LINK]}],
        "firewall_rules": [{"name": "allow-ssh-from-iap"}, {"name": "growin-relay-from-iap"}],
        "effective": {"firewalls": [
            {"name": "allow-ssh-from-iap", "direction": "INGRESS", "sourceRanges": [IAP],
             "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]},
            {"name": "growin-relay-from-iap", "direction": "INGRESS", "sourceRanges": [IAP],
             "allowed": [{"IPProtocol": "tcp", "ports": ["8443"]}]},
        ]},
        "remote": {
            "mokutil": "SecureBoot enabled\n",
            "egress": f"{NAT}\n{NAT}\n",
            "ss": (
                "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
                "LISTEN 0 128 [::]:22 [::]:*\n"
                "LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
                "LISTEN 0 4096 127.0.0.54:53 0.0.0.0:*\n"
            ),
            "uv": "uv-installed\n",
        },
        "auth_list": [{"account": DAILY, "status": "ACTIVE"}],
    }


class FakeRunner:
    def __init__(self, world: dict) -> None:
        self.w = world
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        assert argv[0] == "gcloud"
        a = [x for x in argv[1:] if not x.startswith("--format") and not x.startswith("--project")]
        w = self.w
        if a[:2] == ["services", "list"]:
            return json.dumps(w["apis"])
        if a[:3] == ["iam", "service-accounts", "describe"]:
            return json.dumps(w["sa"])
        if a[:4] == ["iam", "service-accounts", "keys", "list"]:
            return json.dumps(w["keys"])
        if a[:3] == ["secrets", "versions", "list"]:
            return json.dumps(w["versions"])
        if a[:2] == ["secrets", "get-iam-policy"]:
            return json.dumps(w["secret_policy"][a[2]])
        if a[:2] == ["projects", "get-iam-policy"]:
            return json.dumps(w["project_policy"])
        if a[:3] == ["config", "get-value", "account"]:
            return w["operator"] + "\n"
        if a[:3] == ["iam", "service-accounts", "get-iam-policy"]:
            return json.dumps(w["sa_policy"][a[3]])
        if a[:3] == ["compute", "instances", "describe"]:
            return json.dumps(w["instance"])
        if a[:3] == ["compute", "project-info", "describe"]:
            return json.dumps(w["project_info"])
        if a[:3] == ["compute", "addresses", "list"]:
            return json.dumps(w["addresses"])
        if a[:3] == ["compute", "firewall-rules", "list"]:
            return json.dumps(w["firewall_rules"])
        if a[:3] == ["compute", "instances", "network-interfaces"]:
            return json.dumps(w["effective"])
        if a[:2] == ["auth", "list"]:
            return json.dumps(w["auth_list"])
        if a[:2] == ["compute", "ssh"]:
            command = next(x for x in argv if x.startswith("--command="))[len("--command="):]
            if command.startswith("mokutil"):
                return w["remote"]["mokutil"]
            if command.startswith("curl"):
                return w["remote"]["egress"]
            if command.startswith("ss "):
                return w["remote"]["ss"]
            if command.startswith("test -x"):
                if w["remote"]["uv"] is None:
                    raise RuntimeError("exit 1")
                return w["remote"]["uv"]
        raise KeyError(argv)


def by_name(results):
    return {r.name: r for r in results}


def statuses(results):
    return {r.name: r.status for r in results}


PRE_NAMES = [
    "apis_enabled", "sa_exists", "sa_no_user_keys",
    "secret_breeze-api-key_version", "secret_breeze-api-secret_version",
    "secret_breeze-api-key_single_accessor", "secret_breeze-api-secret_single_accessor",
    "sa_no_project_roles", "audit_data_read", "operator_tunnel_role", "operator_oslogin_role",
    "operator_sa_user_new", "operator_sa_user_current",
    "daily_tunnel_role_conditioned", "daily_no_other_roles",
]


# ------------------------------------------------------------- merge audit


def audit_policy():
    return {
        "version": 3,
        "etag": "BwX1",
        "bindings": [{"role": "roles/owner", "members": ["user:a@example.invalid"]}],
        "auditConfigs": [{"service": "storage.googleapis.com",
                          "auditLogConfigs": [{"logType": "ADMIN_READ"}]}],
    }


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def test_merge_adds_data_read_and_preserves_the_rest():
    policy = audit_policy()
    original = copy.deepcopy(policy)
    merged, changed = gs.merge_secretmanager_data_read(policy)
    assert changed is True
    assert policy == original  # input untouched
    for key in ("bindings", "version", "etag"):
        assert canon(merged[key]) == canon(original[key])
    assert canon(merged["auditConfigs"][0]) == canon(original["auditConfigs"][0])
    assert merged["auditConfigs"][1] == {
        "service": "secretmanager.googleapis.com", "auditLogConfigs": [{"logType": "DATA_READ"}]
    }


def test_merge_is_idempotent():
    merged, _ = gs.merge_secretmanager_data_read(audit_policy())
    again, changed = gs.merge_secretmanager_data_read(merged)
    assert changed is False
    assert canon(again) == canon(merged)


def test_merge_keeps_admin_read_and_exemptions():
    policy = audit_policy()
    policy["auditConfigs"].append({
        "service": "secretmanager.googleapis.com",
        "auditLogConfigs": [{"logType": "ADMIN_READ", "exemptedMembers": ["user:x@example.invalid"]}],
    })
    merged, changed = gs.merge_secretmanager_data_read(policy)
    assert changed is True
    logs = merged["auditConfigs"][1]["auditLogConfigs"]
    assert {"logType": "ADMIN_READ", "exemptedMembers": ["user:x@example.invalid"]} in logs
    assert {"logType": "DATA_READ"} in logs


def test_merge_without_audit_configs_key():
    policy = audit_policy()
    del policy["auditConfigs"]
    merged, changed = gs.merge_secretmanager_data_read(policy)
    assert changed and len(merged["auditConfigs"]) == 1


def test_merge_refuses_policy_without_etag():
    policy = audit_policy()
    del policy["etag"]
    with pytest.raises(ValueError):
        gs.merge_secretmanager_data_read(policy)


def test_merge_cli_writes_0600(tmp_path, capsys):
    src, dst = tmp_path / "in.json", tmp_path / "out.json"
    src.write_text(json.dumps(audit_policy()))
    assert gs.main(["merge-audit-config", str(src), "-o", str(dst)]) == 0
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert json.loads(dst.read_text())["auditConfigs"][-1]["service"] == "secretmanager.googleapis.com"
    assert capsys.readouterr().out.strip() == "PASS audit_config_merged"


def test_merge_cli_fails_on_missing_etag(tmp_path, capsys):
    policy = audit_policy()
    del policy["etag"]
    src = tmp_path / "in.json"
    src.write_text(json.dumps(policy))
    assert gs.main(["merge-audit-config", str(src), "-o", str(tmp_path / "out.json")]) == 1
    assert "FAIL merge-audit-config: policy has no etag" in capsys.readouterr().out
    assert not (tmp_path / "out.json").exists()


# ----------------------------------------------------------------- pre-window


def test_pre_window_all_pass_in_order():
    results = gs.check_pre_window(CFG, FakeRunner(make_world()))
    assert [r.name for r in results] == PRE_NAMES
    assert all(r.status == "PASS" for r in results), statuses(results)


def flip(fn):
    def apply(w):
        fn(w)
        return w
    return apply


PRE_FLIPS = {
    "sa_no_user_keys": [flip(lambda w: w.update(keys=[{"name": "k1"}]))],
    "secret_breeze-api-key_single_accessor": [
        flip(lambda w: w["secret_policy"]["breeze-api-key"]["bindings"].append(
            {"role": "roles/viewer", "members": ["user:z@example.invalid"]})),
        flip(lambda w: w["secret_policy"]["breeze-api-key"]["bindings"][0]["members"].append(
            "user:z@example.invalid")),
        flip(lambda w: w["secret_policy"]["breeze-api-key"].update(bindings=[])),
    ],
    "secret_breeze-api-secret_single_accessor": [
        flip(lambda w: w["secret_policy"]["breeze-api-secret"]["bindings"].append(
            {"role": "roles/viewer", "members": ["user:z@example.invalid"]})),
    ],
    "sa_no_project_roles": [
        flip(lambda w: w["project_policy"]["bindings"].append(
            {"role": "roles/editor", "members": [f"serviceAccount:{SA}"]})),
    ],
    "audit_data_read": [
        flip(lambda w: w["project_policy"].update(auditConfigs=[])),
        flip(lambda w: w["project_policy"]["auditConfigs"][0].update(
            auditLogConfigs=[{"logType": "ADMIN_READ"}])),
    ],
    "operator_tunnel_role": [
        flip(lambda w: w["project_policy"].update(bindings=[
            b for b in w["project_policy"]["bindings"]
            if not (b["role"] == gs.ROLE_TUNNEL and f"user:{ADMIN}" in b["members"])])),
    ],
    "operator_oslogin_role": [
        flip(lambda w: w["project_policy"].update(bindings=[
            b for b in w["project_policy"]["bindings"] if b["role"] != gs.ROLE_OSADMIN])),
    ],
    "operator_sa_user_new": [flip(lambda w: w["sa_policy"][SA].update(bindings=[]))],
    "operator_sa_user_current": [flip(lambda w: w["sa_policy"][OLD_SA].update(bindings=[]))],
    "daily_tunnel_role_conditioned": [
        flip(lambda w: w["project_policy"].update(bindings=[
            b for b in w["project_policy"]["bindings"] if f"user:{DAILY}" not in b["members"]])),
        flip(lambda w: w["project_policy"]["bindings"][2].pop("condition")),
        flip(lambda w: w["project_policy"]["bindings"][2]["condition"].update(
            expression="destination.port == 22")),
        flip(lambda w: w["project_policy"]["bindings"][2]["condition"].update(
            expression="destination.port == 8443 || destination.port == 22")),
    ],
    "daily_no_other_roles": [
        flip(lambda w: w["project_policy"]["bindings"].append(
            {"role": role, "members": [f"user:{DAILY}"]}))
        for role in ("roles/owner", "roles/editor", "roles/viewer", "roles/compute.osLogin",
                     "roles/compute.osAdminLogin", "roles/iam.serviceAccountTokenCreator")
    ] + [
        flip(lambda w: w["sa_policy"][SA]["bindings"].append(
            {"role": gs.ROLE_SA_USER, "members": [f"user:{DAILY}"]})),
        flip(lambda w: w["sa_policy"][OLD_SA]["bindings"].append(
            {"role": "roles/iam.serviceAccountTokenCreator", "members": [f"user:{DAILY}"]})),
        flip(lambda w: w["secret_policy"]["breeze-api-secret"]["bindings"].append(
            {"role": "roles/viewer", "members": [f"user:{DAILY}"]})),
    ],
}


@pytest.mark.parametrize(
    "name, mutate",
    [(name, m) for name, ms in PRE_FLIPS.items() for m in ms],
)
def test_pre_window_flips_to_fail(name, mutate):
    results = by_name(gs.check_pre_window(CFG, FakeRunner(mutate(make_world()))))
    assert results[name].status == "FAIL", name
    assert results[name].reason  # a fixed phrase


def test_daily_condition_follows_the_configured_relay_port():
    cfg = gs.GcpConfig(PROJECT, ZONE, VM, DAILY, relay_port=9443)
    results = by_name(gs.check_pre_window(cfg, FakeRunner(make_world())))
    assert results["daily_tunnel_role_conditioned"].status == "FAIL"
    assert results["daily_tunnel_role_conditioned"].reason == "daily tunnel condition differs"


def test_unreadable_policy_fails_closed():
    world = make_world()
    runner = FakeRunner(world)
    del world["project_policy"]  # runner raises KeyError for it
    results = by_name(gs.check_pre_window(CFG, runner))
    for name in ("sa_no_project_roles", "audit_data_read", "operator_tunnel_role",
                 "daily_tunnel_role_conditioned", "daily_no_other_roles"):
        assert results[name].status == "FAIL"
        assert results[name].reason == "read failed"


# --------------------------------------------------------------- post-window


def post_world(**remote):
    world = make_world(post=True)
    world["remote"]["ss"] += f"LISTEN 0 4096 {INTERNAL}:8443 0.0.0.0:*\n"
    world["remote"].update(remote)
    return world


POST_NAMES = [
    "instance_sa", "instance_scope_cloud_platform", "secure_boot", "os_login_enabled",
    "no_instance_ssh_keys", "project_ssh_keys_retired", "address_in_use", "icmp_rule_absent",
    "effective_firewall", "remote_secure_boot", "remote_egress_matches", "remote_listeners",
    "remote_uv_installed",
]


def test_post_window_all_pass():
    results = gs.check_post_window(CFG, FakeRunner(post_world()), expect_relay=True)
    assert [r.name for r in results] == POST_NAMES
    assert all(r.status == "PASS" for r in results), statuses(results)


def test_post_window_without_relay_expected():
    world = make_world(post=True)
    results = gs.check_post_window(CFG, FakeRunner(world), expect_relay=False)
    assert statuses(results)["remote_listeners"] == "PASS"


POST_FLIPS = {
    "instance_sa": [flip(lambda w: w["instance"]["serviceAccounts"][0].update(email=OLD_SA))],
    "instance_scope_cloud_platform": [
        flip(lambda w: w["instance"]["serviceAccounts"][0].update(scopes=["x"]))],
    "secure_boot": [flip(lambda w: w["instance"]["shieldedInstanceConfig"].update(enableSecureBoot=False))],
    "os_login_enabled": [flip(lambda w: w["instance"]["metadata"].update(items=[]))],
    "no_instance_ssh_keys": [flip(lambda w: w["instance"]["metadata"]["items"].append(
        {"key": "ssh-keys", "value": "x"}))],
    "project_ssh_keys_retired": [flip(lambda w: w["project_info"]["commonInstanceMetadata"].update(
        items=[{"key": "ssh-keys", "value": "x"}]))],
    "address_in_use": [
        flip(lambda w: w["addresses"][0].update(status="RESERVED", users=[])),
        flip(lambda w: w["addresses"][0].update(users=["https://x/projects/p/zones/z/instances/other"])),
        flip(lambda w: w.update(addresses=[])),
    ],
    "icmp_rule_absent": [flip(lambda w: w["firewall_rules"].append({"name": "default-allow-icmp"}))],
    "effective_firewall": [
        flip(lambda w: w["effective"]["firewalls"].append(
            {"name": "x", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"],
             "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}]})),
        flip(lambda w: w["effective"]["firewalls"].pop()),  # relay rule missing
    ],
    "remote_secure_boot": [flip(lambda w: w["remote"].update(mokutil="SecureBoot disabled\n"))],
    "remote_egress_matches": [
        flip(lambda w: w["remote"].update(egress=f"198.51.100.9\n{NAT}\n")),
        flip(lambda w: w["remote"].update(egress=f"{NAT}\n198.51.100.9\n")),
        flip(lambda w: w["remote"].update(egress="198.51.100.9\n198.51.100.9\n")),
    ],
    "remote_listeners": [
        flip(lambda w: w["remote"].update(ss=w["remote"]["ss"] + "LISTEN 0 1 0.0.0.0:8443 0.0.0.0:*\n")),
        flip(lambda w: w["remote"].update(ss=w["remote"]["ss"] + "LISTEN 0 1 [::]:8443 [::]:*\n")),
        flip(lambda w: w["remote"].update(ss=w["remote"]["ss"] + "LISTEN 0 1 127.0.0.1:8443 0.0.0.0:*\n")),
        flip(lambda w: w["remote"].update(ss=w["remote"]["ss"] + "LISTEN 0 1 0.0.0.0:80 0.0.0.0:*\n")),
        flip(lambda w: w["remote"].update(
            ss=w["remote"]["ss"].replace(f"{INTERNAL}:8443", "10.0.0.99:8443"))),
    ],
    "remote_uv_installed": [flip(lambda w: w["remote"].update(uv=None))],
}


@pytest.mark.parametrize(
    "name, mutate",
    [(name, m) for name, ms in POST_FLIPS.items() for m in ms],
)
def test_post_window_flips_to_fail(name, mutate):
    results = by_name(gs.check_post_window(CFG, FakeRunner(mutate(post_world())), expect_relay=True))
    assert results[name].status == "FAIL", name


def test_post_window_requires_the_relay_listener_when_expected():
    world = make_world(post=True)  # no relay listener on the box
    results = by_name(gs.check_post_window(CFG, FakeRunner(world), expect_relay=True))
    assert results["remote_listeners"].status == "FAIL"
    assert results["remote_listeners"].reason == "relay listener missing"


def test_post_window_relay_listener_is_unexpected_before_the_relay_exists():
    results = by_name(gs.check_post_window(CFG, FakeRunner(post_world()), expect_relay=False))
    assert results["remote_listeners"].status == "FAIL"


def test_project_ssh_keys_block_flag_counts_as_retired():
    world = post_world()
    world["project_info"]["commonInstanceMetadata"]["items"] = [{"key": "ssh-keys", "value": "x"}]
    world["instance"]["metadata"]["items"].append({"key": "block-project-ssh-keys", "value": "TRUE"})
    results = by_name(gs.check_post_window(CFG, FakeRunner(world), expect_relay=True))
    assert results["project_ssh_keys_retired"].status == "PASS"


def test_remote_commands_use_iap_and_the_exact_shape():
    runner = FakeRunner(post_world())
    gs.check_post_window(CFG, runner, expect_relay=True)
    remote = [c for c in runner.calls if c[1:3] == ["compute", "ssh"]]
    assert len(remote) == 4
    for call in remote:
        assert call[:3] == ["gcloud", "compute", "ssh"]
        assert call[3] == VM
        assert call[4:7] == [f"--zone={ZONE}", f"--project={PROJECT}", "--tunnel-through-iap"]
        assert call[7].startswith("--command=") and len(call) == 8


# ------------------------------------------------------------------ external


def test_external_passes_when_all_ports_are_closed():
    seen = []

    def connect(host, port, timeout):
        seen.append((host, port))
        return False

    results = gs.check_external(CFG, FakeRunner(make_world(post=True)), connect)
    assert [r.status for r in results] == ["PASS"] * 3
    assert seen == [(NAT, 22), (NAT, 443), (NAT, 8443)]


@pytest.mark.parametrize("open_port", [22, 443, 8443])
def test_external_fails_when_a_port_is_open(open_port):
    results = gs.check_external(CFG, FakeRunner(make_world(post=True)),
                                lambda h, p, t: p == open_port)
    failed = [r for r in results if r.status == "FAIL"]
    assert [r.name for r in failed] == [f"external_port_{open_port}_closed"]


def test_external_without_an_address_fails():
    world = make_world(post=True)
    world["instance"]["networkInterfaces"][0]["accessConfigs"] = []
    results = gs.check_external(CFG, FakeRunner(world), lambda *a: False)
    assert all(r.status == "FAIL" for r in results)


# ----------------------------------------------------------- mac credentials


def mac_world():
    world = make_world()
    world["operator"] = DAILY  # after the admin window the active account is the daily one
    return world


def mac(world=None, adc=False):
    world = world or mac_world()
    return {r.name: r for r in gs.check_mac_credentials(FakeRunner(world), lambda: adc, CFG)}


def test_mac_credentials_all_pass():
    results = mac()
    assert {n: r.status for n, r in results.items()} == {
        "active_account_is_daily": "PASS", "no_other_gcloud_accounts": "PASS", "no_adc_file": "PASS"}


def test_mac_credentials_flag_admin_leftovers_without_naming_accounts():
    world = mac_world()
    world["auth_list"].append({"account": ADMIN, "status": ""})
    results = mac(world)
    assert results["no_other_gcloud_accounts"].status == "FAIL"
    assert results["no_other_gcloud_accounts"].reason == "admin credentials still on this Mac"

    world = mac_world()
    world["operator"] = ADMIN
    assert mac(world)["active_account_is_daily"].status == "FAIL"

    assert mac(adc=True)["no_adc_file"].status == "FAIL"
    for r in list(mac(world, adc=True).values()):
        assert ADMIN not in r.reason and DAILY not in r.reason


def test_mac_credentials_uses_only_local_gcloud_state():
    runner = FakeRunner(mac_world())
    gs.check_mac_credentials(runner, lambda: False, CFG)
    commands = [tuple(c[1:3]) for c in runner.calls]
    assert set(commands) <= {("config", "get-value"), ("auth", "list")}


# -------------------------------------------------------------- daily access


class Prober:
    def __init__(self, ssh=(255, "Permission denied"), t22=(1, "ERROR 4033 not authorized"),
                 relay=(1, "Error while connecting [4003: 'failed to connect to backend']")):
        self.answers = [ssh, t22, relay]
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        assert timeout > 0
        return self.answers[len(self.calls) - 1]


def daily(prober, roles_ok=True):
    return {r.name: r for r in gs.check_daily_access(CFG, prober, daily_roles_ok=roles_ok)}


def test_daily_access_expected_outcome_passes():
    results = daily(Prober())
    assert {n: r.status for n, r in results.items()} == {
        "daily_cannot_ssh": "PASS", "daily_cannot_tunnel_ssh_port": "PASS",
        "daily_relay_port_iam": "PASS", "actas_not_required": "PASS"}
    assert results["actas_not_required"].reason == "tunnel path does not need actAs"


def test_daily_access_exactly_three_attempts_as_the_daily_identity():
    prober = Prober()
    daily(prober)
    ssh, t22, relay = prober.calls
    assert ssh[:4] == ["gcloud", "compute", "ssh", VM]
    assert "--tunnel-through-iap" in ssh and "--command=true" in ssh and "--quiet" in ssh
    assert t22[:5] == ["gcloud", "compute", "start-iap-tunnel", VM, "22"]
    assert relay[:5] == ["gcloud", "compute", "start-iap-tunnel", VM, "8443"]
    ports = []
    for call in prober.calls:
        assert f"--account={DAILY}" in call
        assert f"--zone={ZONE}" in call and f"--project={PROJECT}" in call
        words = " ".join(call).replace("-", " ").split()
        assert not {"create", "delete", "add", "remove", "set", "update", "stop", "enable",
                    "disable"} & set(words)
    for call in (t22, relay):
        local = next(x for x in call if x.startswith("--local-host-port=127.0.0.1:"))
        ports.append(int(local.rsplit(":", 1)[1]))
    assert all(1024 <= p <= 65535 for p in ports)


def test_daily_can_ssh_fails():
    assert daily(Prober(ssh=(0, "")))["daily_cannot_ssh"].status == "FAIL"


def test_daily_listening_on_ssh_port_fails_even_when_killed():
    results = daily(Prober(t22=(-15, "Listening on port [51234].")))
    assert results["daily_cannot_tunnel_ssh_port"].status == "FAIL"


def test_daily_relay_tunnel_listening_passes():
    assert daily(Prober(relay=(-15, "Listening on port [51234].")))["daily_relay_port_iam"].status == "PASS"


@pytest.mark.parametrize("text", [
    "ERROR: (gcloud.compute.start-iap-tunnel) Error while connecting [4033: 'not authorized'].",
    "Permission denied on resource",
    "access Denied",
])
def test_daily_relay_denied_by_iam_fails(text):
    results = daily(Prober(relay=(1, text)))
    assert results["daily_relay_port_iam"].status == "FAIL"
    assert results["daily_relay_port_iam"].reason == "tunnel denied by IAM"
    assert results["actas_not_required"].status == "FAIL"
    assert results["actas_not_required"].reason == "daily tunnel needs more than the conditioned iap role"


def test_daily_relay_inconclusive_output_fails_closed():
    assert daily(Prober(relay=(1, "something odd")))["daily_relay_port_iam"].status == "FAIL"


def test_actas_needs_the_recorded_roles_result():
    assert daily(Prober(), roles_ok=False)["actas_not_required"].status == "FAIL"
    assert daily(Prober(), roles_ok=None)["actas_not_required"].reason == "daily_no_other_roles not confirmed"


# ----------------------------------------------------------------------- CLI


def write_cfg(tmp_path, **gcp):
    base = {"project": PROJECT, "zone": ZONE, "instance": VM, "daily_account": DAILY}
    base.update(gcp)
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps({"gcp": base, "tunnel": {"relay_port": 8443}, "listener": {"window_s": 120}}))
    return path


def test_cli_output_is_value_free_and_exits_1_on_fail(tmp_path, capsys):
    world = post_world()
    world["addresses"][0]["status"] = "RESERVED"
    code = gs.main(["check", "post-window", "--config", str(write_cfg(tmp_path)), "--expect-relay"],
                   runner=FakeRunner(world))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL address_in_use: reserved address is not in use by the vm" in out
    assert "PASS instance_sa" in out
    for value in VALUES:
        assert value not in out
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", out)
    for line in out.strip().splitlines():
        assert re.match(r"^(PASS \S+|(WARN|FAIL) \S+: .+)$", line), line


def test_cli_pre_window_pass_exit_0(tmp_path, capsys):
    code = gs.main(["check", "pre-window", "--config", str(write_cfg(tmp_path))],
                   runner=FakeRunner(make_world()))
    out = capsys.readouterr().out
    assert code == 0
    assert out.count("PASS ") == len(PRE_NAMES)
    for value in VALUES:
        assert value not in out


def test_cli_external_mac_and_daily_groups(tmp_path, capsys):
    cfg = str(write_cfg(tmp_path))
    assert gs.main(["check", "external", "--config", cfg], runner=FakeRunner(make_world(post=True)),
                   connect=lambda *a: False) == 0
    assert gs.main(["check", "mac-credentials", "--config", cfg], runner=FakeRunner(mac_world()),
                   adc_exists=lambda: False) == 0
    assert gs.main(["check", "daily-access", "--config", cfg, "--daily-roles-ok"],
                   prober=Prober()) == 0
    out = capsys.readouterr().out
    for value in VALUES:
        assert value not in out


def test_cli_refuses_placeholder_config(tmp_path, capsys):
    cfg = write_cfg(tmp_path, project="<GCP_PROJECT_ID>")
    assert gs.main(["check", "pre-window", "--config", str(cfg)], runner=FakeRunner(make_world())) == 1
    assert "FAIL config" in capsys.readouterr().out


@pytest.mark.parametrize("field", ["project", "zone", "instance", "daily_account"])
def test_config_rejects_option_like_and_blank_values(tmp_path, field):
    for bad in ("--evil", "", "has space"):
        with pytest.raises(ValueError):
            gs.load_config(write_cfg(tmp_path, **{field: bad}))


# ---------------------------------------------------- read-only guarantee


MUTATING = {"create", "delete", "stop", "start", "enable", "disable", "update"}


def test_no_runner_call_contains_a_mutating_verb(tmp_path):
    runner = FakeRunner(post_world())
    gs.check_pre_window(CFG, runner)
    gs.check_post_window(CFG, runner, expect_relay=True)
    gs.check_external(CFG, runner, lambda *a: False)
    gs.check_mac_credentials(runner, lambda: False, CFG)
    assert runner.calls
    for call in runner.calls:
        for token in call[1:]:
            words = token.replace("--command=", "").split()
            for word in words:
                assert word not in MUTATING, call
                assert not word.startswith(("add-", "remove-", "set-")), call


def test_real_runner_refuses_anything_but_gcloud():
    with pytest.raises(ValueError):
        gs.real_runner(["rm", "-rf", "x"])
    with pytest.raises(ValueError):
        gs.real_prober(["bash", "-c", "true"], 1.0)


# ------------------------------------------------------------ example config


def test_example_config_shape_and_placeholders():
    data = json.loads((ROOT / "gateway" / "ops" / "gateway.example.json").read_text())
    assert set(data) == {"gcp", "tunnel", "listener"}
    assert set(data["gcp"]) == {"project", "zone", "instance", "daily_account"}
    assert all(re.fullmatch(r"<[A-Z_]+>", v) for v in data["gcp"].values())
    assert data["tunnel"] == {"relay_port": 8443, "ready_timeout_s": 30}
    assert data["listener"] == {"window_s": 120}


def test_example_config_is_refused_until_filled_in():
    with pytest.raises(ValueError):
        gs.load_config(ROOT / "gateway" / "ops" / "gateway.example.json")
