"""Admin CLI for the VM order state (D-05, T-63-08). Run from the VM shell only.

    python -m gateway_vm.orders.admin STATE_DIR status
    python -m gateway_vm.orders.admin STATE_DIR verify-audit
    python -m gateway_vm.orders.admin STATE_DIR reset --latch halt|stop|mac_halt|account_mismatch [--isin ISIN]
    python -m gateway_vm.orders.admin STATE_DIR session-end-check

There is no HTTP route that does any of this: no route clears a latch. The
`ended` latch is terminal and `reset` refuses it. `reset` refuses while the
audit chain is broken, because a reset that cannot be recorded must not happen.
`session-end-check` reads persisted state only; it may over-alert on a stale
ledger and never suppresses an alert. It exits non-zero if an alert or its
audit entry failed. The alert port defaults to an unbound one that fails
loudly until 63-05 binds the real channel.

Exit codes: 0 ok, 1 refused or failed, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from . import OrderRefusal
from .audit import AuditBroken, AuditLog
from .risk import (
    AlertPort,
    ResetRefused,
    UnboundAlertPort,
    reset_latch,
    session_end_check,
)
from .store import AUDIT_FILE, StateStore

_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _print(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, sort_keys=True) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
    alert_port: AlertPort | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = argparse.ArgumentParser(prog="gateway_vm.orders.admin")
    parser.add_argument("state_dir")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("verify-audit")
    sub.add_parser("session-end-check")
    reset = sub.add_parser("reset")
    reset.add_argument(
        "--latch", required=True, choices=("halt", "stop", "mac_halt", "account_mismatch", "ended")
    )
    reset.add_argument("--isin")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code not in (0, None) else 0

    store = StateStore(args.state_dir)
    audit = AuditLog(Path(args.state_dir) / AUDIT_FILE, clock=clock)

    try:
        if args.command == "verify-audit":
            count, last = audit.verify()
            _print(out, {"ok": True, "entries": count, "last_sha256": last})
            return 0

        if args.command == "status":
            with store.lock():
                state = store.load()
            try:
                entries, _ = audit.verify()
                audit_ok = True
            except AuditBroken:
                entries, audit_ok = 0, False
            _print(
                out,
                {
                    "latches": list(state.latch_names()),
                    "stops": sorted(state.stops),
                    "drawdown": format(state.drawdown, "f"),
                    "peak": format(state.peak, "f"),
                    "peak_date": state.peak_date,
                    "last_evaluated_session": state.last_evaluated_session,
                    "consumed_intents": len(state.consumed_intents),
                    "audit_entries": entries,
                    "audit_ok": audit_ok,
                },
            )
            return 0 if audit_ok else 1

        if args.command == "reset":
            if args.latch == "ended":
                err.write("refused: the pilot-ended latch is terminal and cannot be reset\n")
                return 1
            if args.isin is not None and (args.latch != "stop" or _ISIN.fullmatch(args.isin) is None):
                err.write("refused: --isin applies to --latch stop and must be a valid ISIN\n")
                return 1
            with store.lock():
                audit.verify()  # raises AuditBroken: no reset on a broken chain
                state = store.load()
                reset_latch(state, args.latch, args.isin)
                # Record first: a reset that cannot be audited must not happen.
                audit.append(
                    {
                        "route": "admin",
                        "decision": "RESET",
                        "codes": [args.latch],
                        "isin": args.isin,
                        "latches": list(state.latch_names()),
                    }
                )
                store.save(state)
            _print(out, {"reset": args.latch, "latches": list(state.latch_names())})
            return 0

        if args.command == "session-end-check":
            with store.lock():
                state = store.load()
                result = session_end_check(
                    state, clock, alert_port or UnboundAlertPort(), audit=audit
                )
                if result.sent:
                    store.save(state)
            _print(
                out,
                {
                    "alerts_sent": len(result.sent),
                    "alerts_failed": len(result.failed),
                    "audit_failed": result.audit_failed,
                },
            )
            return 0 if result.ok else 1
    except AuditBroken:
        err.write("refused: the audit chain is broken or unwritable\n")
        return 1
    except ResetRefused as exc:
        err.write(f"refused: {exc}\n")
        return 1
    except OrderRefusal as exc:
        err.write(f"failed: {exc.code}\n")
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
