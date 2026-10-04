"""verify_firewall: effective-firewall classification, offline, inline fixtures."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "ops") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "ops"))

import verify_firewall as vf  # noqa: E402

IAP = "35.235.240.0/20"


def vpc(name, sources, allowed, **extra):
    rule = {"name": name, "direction": "INGRESS", "priority": 1000,
            "sourceRanges": sources, "allowed": allowed}
    rule.update(extra)
    return rule


def policy_rule(src, layer4, action="allow", direction="INGRESS", **extra):
    rule = {"direction": direction, "action": action, "priority": 100,
            "match": {"srcIpRanges": src, "layer4Configs": layer4}}
    rule.update(extra)
    return rule


SSH_IAP = vpc("allow-ssh-from-iap", [IAP], [{"IPProtocol": "tcp", "ports": ["22"]}])
INTERNAL = vpc(
    "default-allow-internal", ["10.128.0.0/9"],
    [{"IPProtocol": "tcp", "ports": ["0-65535"]},
     {"IPProtocol": "udp", "ports": ["0-65535"]},
     {"IPProtocol": "icmp"}],
)
ICMP = vpc("default-allow-icmp", ["0.0.0.0/0"], [{"IPProtocol": "icmp"}])
RELAY_IAP = vpc(
    "allow-relay-from-iap", [IAP], [{"IPProtocol": "tcp", "ports": ["8443"]}],
    targetServiceAccounts=["breeze-gateway@example.invalid"],
)


def fw(*rules, policies=None):
    data = {"firewalls": list(rules)}
    if policies is not None:
        data["firewallPolicys"] = policies
    return data


def fails(findings):
    return [f for f in findings if f.level == "FAIL"]


def warns(findings):
    return [f for f in findings if f.level == "WARN"]


# ------------------------------------------------------------ today's fixture


def test_todays_vm_has_exactly_one_fail_for_icmp():
    findings = vf.evaluate(fw(SSH_IAP, INTERNAL, ICMP), allow_icmp=False)
    assert [(f.rule, f.reason) for f in fails(findings)] == [("default-allow-icmp", "public icmp")]
    assert warns(findings) == []


def test_allow_icmp_downgrades_to_one_warn():
    findings = vf.evaluate(fw(SSH_IAP, INTERNAL, ICMP), allow_icmp=True)
    assert fails(findings) == []
    assert [(f.rule, f.reason) for f in warns(findings)] == [("default-allow-icmp", "public icmp")]


# ----------------------------------------------------------------- FAIL cases


@pytest.mark.parametrize(
    "rule",
    [
        vpc("ssh-open", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["22"]}]),
        vpc("range-open", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["20-30"]}]),
        vpc("all-open", ["0.0.0.0/0"], [{"IPProtocol": "all"}]),
        vpc("rdp-v6", ["::/0"], [{"IPProtocol": "tcp", "ports": ["3389"]}]),
        vpc("https-open", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["443"]}]),
        vpc("odd-range", ["8.8.8.0/24"], [{"IPProtocol": "tcp", "ports": ["22"]}]),
        vpc("udp-open", ["0.0.0.0/0"], [{"IPProtocol": "udp"}]),
        vpc("no-ports-means-all", ["0.0.0.0/0"], [{"IPProtocol": "tcp"}]),
        vpc("esp-open", ["0.0.0.0/0"], [{"IPProtocol": "esp"}]),
    ],
)
def test_public_ingress_fails(rule):
    findings = vf.evaluate(fw(rule), allow_icmp=False)
    assert fails(findings), rule["name"]
    assert all(f.rule == rule["name"] for f in fails(findings))


def test_policy_allow_all_from_public_fails():
    data = fw(policies=[{"rules": [policy_rule(["0.0.0.0/0"], [{"ipProtocol": "all"}])]}])
    assert fails(vf.evaluate(data, allow_icmp=False))


def test_policy_rule_without_layer4_matches_all_protocols():
    data = fw(policies=[{"rules": [{"direction": "INGRESS", "action": "allow",
                                    "match": {"srcIpRanges": ["0.0.0.0/0"]}}]}])
    assert fails(vf.evaluate(data, allow_icmp=False))


# ---------------------------------------------------------------- not counted


@pytest.mark.parametrize(
    "rule",
    [
        vpc("off", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["22"]}], disabled=True),
        vpc("egress", ["0.0.0.0/0"], [{"IPProtocol": "all"}], direction="EGRESS"),
        vpc("iap-only", [IAP], [{"IPProtocol": "tcp", "ports": ["22"]}]),
        vpc("rfc1918", ["192.168.0.0/16", "10.1.0.0/16", "172.16.5.0/24"],
            [{"IPProtocol": "tcp", "ports": ["0-65535"]}]),
        {"name": "deny-rule", "direction": "INGRESS", "sourceRanges": ["0.0.0.0/0"],
         "denied": [{"IPProtocol": "all"}]},
    ],
)
def test_ignored_rules_do_not_fail(rule):
    assert fails(vf.evaluate(fw(rule), allow_icmp=False)) == []


@pytest.mark.parametrize(
    "extra",
    [{"action": "deny"}, {"action": "goto_next"}, {"disabled": True}, {"direction": "EGRESS"}],
)
def test_ignored_policy_rules_do_not_fail(extra):
    rule = policy_rule(["0.0.0.0/0"], [{"ipProtocol": "all"}])
    rule.update(extra)
    assert fails(vf.evaluate(fw(policies=[{"rules": [rule]}]), allow_icmp=False)) == []


# ----------------------------------------------------------------------- WARN


def test_iap_range_on_other_ports_warns():
    rule = vpc("iap-web", [IAP], [{"IPProtocol": "tcp", "ports": ["80"]}])
    findings = vf.evaluate(fw(rule), allow_icmp=False)
    assert fails(findings) == []
    assert [(f.rule, f.reason) for f in warns(findings)] == [("iap-web", "iap range on non-ssh port")]


def test_iap_range_on_ssh_only_does_not_warn():
    assert vf.evaluate(fw(SSH_IAP), allow_icmp=False) == []


def test_relay_port_is_not_a_warn_when_given():
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP), allow_icmp=False, relay_port=8443)
    assert findings == []


def test_relay_port_warns_when_not_given():
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP), allow_icmp=False)
    assert [f.rule for f in warns(findings)] == ["allow-relay-from-iap"]


# ------------------------------------------------------------------ relay port


def test_default_allow_internal_makes_the_relay_port_fail():
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP, INTERNAL), allow_icmp=True, relay_port=8443)
    assert [(f.rule, f.reason) for f in fails(findings)] == [
        ("default-allow-internal", "relay port reachable beyond the iap range")
    ]


@pytest.mark.parametrize(
    "rule",
    [
        vpc("rfc-range", ["10.0.0.0/8"], [{"IPProtocol": "tcp", "ports": ["8000-9000"]}]),
        vpc("public-relay", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["8443"]}]),
        vpc("all-protocols", ["10.0.0.0/8"], [{"IPProtocol": "all"}]),
        vpc("tag-sourced", [], [{"IPProtocol": "tcp", "ports": ["8443"]}],
            sourceTags=["web"]),
    ],
)
def test_other_rules_covering_the_relay_port_fail(rule):
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP, rule), allow_icmp=True, relay_port=8443)
    reasons = {(f.rule, f.reason) for f in fails(findings)}
    assert (rule["name"], "relay port reachable beyond the iap range") in reasons


def test_policy_rule_with_iap_plus_another_range_fails():
    pol = policy_rule([IAP, "10.0.0.0/8"], [{"ipProtocol": "tcp", "ports": ["8443"]}],
                      name="pol-relay")
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP, policies=[{"rules": [pol]}]),
                           allow_icmp=True, relay_port=8443)
    assert ("pol-relay", "relay port reachable beyond the iap range") in {
        (f.rule, f.reason) for f in fails(findings)
    }


@pytest.mark.parametrize(
    "rule",
    [
        vpc("udp-only", ["10.0.0.0/8"], [{"IPProtocol": "udp", "ports": ["8443"]}]),
        vpc("icmp-only", ["10.0.0.0/8"], [{"IPProtocol": "icmp"}]),
        vpc("egress-8443", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["8443"]}],
            direction="EGRESS"),
        vpc("other-port", ["10.0.0.0/8"], [{"IPProtocol": "tcp", "ports": ["9000-9100"]}]),
    ],
)
def test_rules_that_do_not_cover_the_relay_port_are_ignored(rule):
    findings = vf.evaluate(fw(SSH_IAP, RELAY_IAP, rule), allow_icmp=True, relay_port=8443)
    assert fails(findings) == []


def test_missing_relay_rule_fails():
    findings = vf.evaluate(fw(SSH_IAP), allow_icmp=False, relay_port=8443)
    assert [(f.rule, f.reason) for f in fails(findings)] == [("relay-port", "relay port rule missing")]


def test_relay_checks_do_not_run_without_a_relay_port():
    findings = vf.evaluate(fw(SSH_IAP, INTERNAL, ICMP), allow_icmp=False)
    assert [f.reason for f in findings] == ["public icmp"]


# ----------------------------------------------------------- unrecognized input


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"something": []},
        [],
        None,
        "text",
        {"firewalls": "nope"},
        {"firewalls": ["not-a-dict"]},
        {"firewalls": [{"name": "x", "sourceRanges": ["not-a-cidr"],
                        "allowed": [{"IPProtocol": "tcp"}]}]},
        {"firewalls": [{"name": "x", "sourceRanges": ["0.0.0.0/0"],
                        "allowed": [{"IPProtocol": "tcp", "ports": ["abc"]}]}]},
        {"firewalls": [], "firewallPolicys": [{"rules": ["bad"]}]},
        {"firewallPolicys": [{"rules": [{"action": "allow", "match": "oops"}]}]},
    ],
)
def test_unrecognized_shapes_fail_closed(data):
    findings = vf.evaluate(data, allow_icmp=True, relay_port=8443)
    assert [(f.level, f.reason) for f in findings] == [("FAIL", "unrecognized shape")]


def test_only_one_of_the_two_keys_is_enough():
    assert vf.evaluate({"firewallPolicys": []}, allow_icmp=False) == []
    assert vf.evaluate({"firewalls": []}, allow_icmp=False) == []


# ------------------------------------------------------------------------- CLI


def run_cli(tmp_path, capsys, data, *extra):
    path = tmp_path / "fw.json"
    path.write_text(json.dumps(data))
    code = vf.main([str(path), *extra])
    return code, capsys.readouterr().out


def test_cli_pass(tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, fw(SSH_IAP, INTERNAL), "--allow-icmp")
    assert code == 0
    assert "PASS effective-firewall" in out


def test_cli_fail_lines_and_no_addresses(tmp_path, capsys):
    data = fw(SSH_IAP, INTERNAL, ICMP,
              vpc("ssh-open", ["0.0.0.0/0"], [{"IPProtocol": "tcp", "ports": ["22"]}]))
    code, out = run_cli(tmp_path, capsys, data)
    assert code == 1
    lines = out.strip().splitlines()
    assert "FAIL default-allow-icmp: public icmp" in lines
    assert "FAIL ssh-open: public ingress tcp" in lines
    assert not any(line.startswith("PASS") for line in lines)
    # no IP address in the output at all (the IAP range constant is never printed either)
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", out)


def test_cli_relay_port_flag(tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, fw(SSH_IAP), "--relay-port", "8443")
    assert code == 1
    assert "FAIL relay-port: relay port rule missing" in out


def test_cli_reads_stdin(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(fw(SSH_IAP))))
    assert vf.main(["-"]) == 0
    assert "PASS effective-firewall" in capsys.readouterr().out


def test_cli_unreadable_or_invalid_json_fails_closed(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert vf.main([str(bad)]) == 1
    assert "FAIL effective-firewall: unrecognized shape" in capsys.readouterr().out
    assert vf.main([str(tmp_path / "missing.json")]) == 1
