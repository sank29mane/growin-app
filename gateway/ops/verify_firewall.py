#!/usr/bin/env python3
"""Verify the effective firewall of the gateway VM (GATE-01, D-14, OD-16).

Reads the JSON from `gcloud compute instances network-interfaces
get-effective-firewalls ... --format=json` (a path, or "-" for stdin) and
fails on any effective ingress allow rule that a public source can reach. With
--relay-port it also requires that the relay port is reachable from the IAP
range only, through at least one rule.

Output is one line per finding, "FAIL <rule>: <reason>" or "WARN <rule>:
<reason>", then "PASS effective-firewall" when nothing failed. Reasons are
fixed phrases. No address or port list is ever printed. Stdlib only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from typing import Sequence

IAP_RANGE = ipaddress.ip_network("35.235.240.0/20")
PRIVATE_RANGES = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SSH_PORT = 22
_PORT_RE = re.compile(r"^(\d{1,5})(?:-(\d{1,5}))?$")
_ALL_PORTS = [(0, 65535)]


@dataclass(frozen=True)
class Finding:
    level: str  # FAIL | WARN
    rule: str
    reason: str  # a fixed phrase, never an address


class _Shape(Exception):
    """The JSON is not a shape this verifier understands."""


@dataclass(frozen=True)
class _Allow:
    name: str
    sources: tuple[ipaddress._BaseNetwork, ...]
    protocols: tuple[tuple[str, list[tuple[int, int]]], ...]  # (protocol, port ranges)


def _is_subnet(net: ipaddress._BaseNetwork, parent: ipaddress._BaseNetwork) -> bool:
    return net.version == parent.version and net.subnet_of(parent)


def _is_iap(net: ipaddress._BaseNetwork) -> bool:
    return _is_subnet(net, IAP_RANGE)


def _is_safe(net: ipaddress._BaseNetwork) -> bool:
    return _is_iap(net) or any(_is_subnet(net, private) for private in PRIVATE_RANGES)


def _parse_ports(raw: object) -> list[tuple[int, int]]:
    if raw is None or raw == []:
        return list(_ALL_PORTS)
    if not isinstance(raw, list):
        raise _Shape
    out: list[tuple[int, int]] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, (str, int)):
            raise _Shape
        match = _PORT_RE.match(str(entry))
        if not match:
            raise _Shape
        low = int(match.group(1))
        high = int(match.group(2) or match.group(1))
        if low > high or high > 65535:
            raise _Shape
        out.append((low, high))
    return out


def _parse_sources(raw: object) -> tuple[ipaddress._BaseNetwork, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _Shape
    nets = []
    for entry in raw:
        if not isinstance(entry, str):
            raise _Shape
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise _Shape from None
    return tuple(nets)


def _normalize_protocol(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise _Shape
    return raw.lower()


def _vpc_rule(rule: object) -> _Allow | None:
    if not isinstance(rule, dict):
        raise _Shape
    if rule.get("disabled") is True:
        return None
    if str(rule.get("direction", "INGRESS")).upper() != "INGRESS":
        return None
    allowed = rule.get("allowed")
    if allowed is None:
        return None  # a deny rule or a rule with nothing allowed
    if not isinstance(allowed, list):
        raise _Shape
    protocols = []
    for entry in allowed:
        if not isinstance(entry, dict):
            raise _Shape
        protocols.append(
            (_normalize_protocol(entry.get("IPProtocol")), _parse_ports(entry.get("ports")))
        )
    return _Allow(
        str(rule.get("name", "unnamed-rule")),
        _parse_sources(rule.get("sourceRanges")),
        tuple(protocols),
    )


def _policy_rule(rule: object, index: int) -> _Allow | None:
    if not isinstance(rule, dict):
        raise _Shape
    if rule.get("disabled") is True:
        return None
    if str(rule.get("direction", "INGRESS")).upper() != "INGRESS":
        return None
    if str(rule.get("action", "")).lower() != "allow":
        return None
    match = rule.get("match")
    if not isinstance(match, dict):
        raise _Shape
    configs = match.get("layer4Configs")
    if configs is None or configs == []:
        protocols: list[tuple[str, list[tuple[int, int]]]] = [("all", list(_ALL_PORTS))]
    elif isinstance(configs, list):
        protocols = []
        for entry in configs:
            if not isinstance(entry, dict):
                raise _Shape
            protocols.append(
                (_normalize_protocol(entry.get("ipProtocol")), _parse_ports(entry.get("ports")))
            )
    else:
        raise _Shape
    name = rule.get("name") or rule.get("ruleName") or f"policy-rule-{index}"
    return _Allow(str(name), _parse_sources(match.get("srcIpRanges")), tuple(protocols))


def _collect(data: object) -> list[_Allow]:
    if not isinstance(data, dict):
        raise _Shape
    has_vpc = "firewalls" in data
    has_policy = "firewallPolicys" in data
    if not (has_vpc or has_policy):
        raise _Shape
    rules: list[_Allow] = []
    if has_vpc:
        vpc = data["firewalls"]
        if not isinstance(vpc, list):
            raise _Shape
        for rule in vpc:
            parsed = _vpc_rule(rule)
            if parsed:
                rules.append(parsed)
    if has_policy:
        policies = data["firewallPolicys"]
        if not isinstance(policies, list):
            raise _Shape
        index = 0
        for policy in policies:
            if not isinstance(policy, dict):
                raise _Shape
            policy_rules = policy.get("rules", [])
            if not isinstance(policy_rules, list):
                raise _Shape
            for rule in policy_rules:
                index += 1
                parsed = _policy_rule(rule, index)
                if parsed:
                    rules.append(parsed)
    return rules


def _covers(ports: list[tuple[int, int]], port: int) -> bool:
    return any(low <= port <= high for low, high in ports)


def _has_other_than(ports: list[tuple[int, int]], allowed: set[int]) -> bool:
    """True when the ranges include any port outside `allowed`."""
    for low, high in ports:
        count = high - low + 1
        inside = sum(1 for p in allowed if low <= p <= high)
        if count > inside:
            return True
    return False


def evaluate(data: object, *, allow_icmp: bool, relay_port: int | None = None) -> list[Finding]:
    try:
        rules = _collect(data)
    except _Shape:
        return [Finding("FAIL", "effective-firewall", "unrecognized shape")]

    findings: list[Finding] = []

    def add(level: str, rule: str, reason: str) -> None:
        finding = Finding(level, rule, reason)
        if finding not in findings:
            findings.append(finding)

    relay_rule_found = False
    for rule in rules:
        public = any(not _is_safe(net) for net in rule.sources)
        iap_only = bool(rule.sources) and all(_is_iap(net) for net in rule.sources)
        names = {proto for proto, _ports in rule.protocols}

        if public:
            if "all" in names:
                add("FAIL", rule.name, "public ingress all protocols")
            if "tcp" in names:
                add("FAIL", rule.name, "public ingress tcp")
            if "udp" in names:
                add("FAIL", rule.name, "public ingress udp")
            if "icmp" in names or "icmpv6" in names or "58" in names or "1" in names:
                add("WARN" if allow_icmp else "FAIL", rule.name, "public icmp")
            other = names - {"all", "tcp", "udp", "icmp", "icmpv6", "58", "1"}
            if other:
                add("FAIL", rule.name, "public ingress other protocol")

        if iap_only:
            allowed_ports = {SSH_PORT} | ({relay_port} if relay_port is not None else set())
            for proto, ports in rule.protocols:
                if proto == "all" or (proto == "tcp" and _has_other_than(ports, allowed_ports)):
                    add("WARN", rule.name, "iap range on non-ssh port")

        if relay_port is not None:
            covers = any(
                proto == "all" or (proto == "tcp" and _covers(ports, relay_port))
                for proto, ports in rule.protocols
            )
            if covers:
                if iap_only:
                    relay_rule_found = True
                else:
                    add("FAIL", rule.name, "relay port reachable beyond the iap range")

    if relay_port is not None and not relay_rule_found:
        add("FAIL", "relay-port", "relay port rule missing")
    return findings


def render(findings: Sequence[Finding]) -> list[str]:
    lines = [f"{f.level} {f.rule}: {f.reason}" for f in findings]
    if not any(f.level == "FAIL" for f in findings):
        lines.append("PASS effective-firewall")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify effective firewall rules (GATE-01).")
    parser.add_argument("path", help='JSON file from get-effective-firewalls, or "-" for stdin')
    parser.add_argument("--allow-icmp", action="store_true", help="downgrade public ICMP to WARN")
    parser.add_argument("--relay-port", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        text = sys.stdin.read() if args.path == "-" else open(args.path, encoding="utf-8").read()
        data = json.loads(text)
    except (OSError, ValueError):
        findings = [Finding("FAIL", "effective-firewall", "unrecognized shape")]
    else:
        findings = evaluate(data, allow_icmp=args.allow_icmp, relay_port=args.relay_port)
    for line in render(findings):
        print(line)
    return 1 if any(f.level == "FAIL" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
