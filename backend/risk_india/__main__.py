"""Operator CLI for the Mac's India latches (Phase 63-04, D-05, T-63-24).

    python -m risk_india status
    python -m risk_india reset --latch halt --confirm [--rebase-halt-anchor] [--actor NAME]
    python -m risk_india reset --latch stop --confirm [--isin KEY]

Run it from the backend directory with ``PYTHONPATH=backend``. It reads
``private/india/`` (``--private-dir``, else ``GROWIN_PRIVATE_DIR``, else ``<repo>/private``)
and the India ledger path (``--ledger``, else the workspace default) only to place and
validate the latch file. It opens the ledger read-only, to learn whether any fill exists.

``reset`` clears ``halt`` or a ``stop`` latch and nothing else. ``ended`` is terminal for the
pilot and is refused. Every reset is written to the audit trail (``log_audit``, workspace
india) BEFORE the latch file changes, so a reset that cannot be audited does not happen. A
refused reset is audited too. Without ``--confirm`` nothing runs.

Exit codes: 0 done, 1 refused or unreadable state, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from execution.ledger import LedgerReader, default_ledger_path
from private_config import PrivateConfigError, load_workspace_config
from risk_india.drawdown import ResetRefused
from risk_india.rules import RiskConfigError
from risk_india.state import (
    LatchStore,
    StateUnreadable,
    StateUnwritable,
    limits_from_config,
    state_path_for,
)

Audit = Callable[..., Any]


def _default_private_dir() -> Path:
    configured = os.environ.get("GROWIN_PRIVATE_DIR")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "private"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m risk_india", description=__doc__.split("\n")[0])
    parser.add_argument("--private-dir", type=Path, default=None)
    parser.add_argument("--ledger", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="print the latch names and the open exits")
    reset = commands.add_parser("reset", help="clear the halt latch or a stop latch")
    reset.add_argument("--latch", required=True, choices=("halt", "stop", "ended"))
    reset.add_argument("--isin", default=None, help="stop latch key; omit to clear every stop")
    reset.add_argument(
        "--rebase-halt-anchor",
        action="store_true",
        help="accept the loss: restart the -8%% test from the current equity (halt only)",
    )
    reset.add_argument("--actor", default=None, help="who is resetting (default: the OS user)")
    reset.add_argument("--confirm", action="store_true", help="required to change anything")
    return parser


def _store(args: argparse.Namespace) -> LatchStore:
    private_dir = args.private_dir or _default_private_dir()
    ledger_path = args.ledger or default_ledger_path("india")
    config = load_workspace_config(private_dir, "india", require_india_execution=True)
    limits = limits_from_config(config)

    def has_fills() -> bool:
        if not Path(ledger_path).exists():
            return False  # no ledger, so no fill: a pilot that has not started
        with LedgerReader(ledger_path, workspace="india") as reader:
            return reader.has_fills()

    return LatchStore(state_path_for(ledger_path), limits, has_fills=has_fills)


def main(argv: Sequence[str] | None = None, *, audit: Audit | None = None) -> int:
    args = _parser().parse_args(argv)
    if audit is None:
        from utils.audit_log import log_audit

        audit = log_audit
    try:
        store = _store(args)
    except (PrivateConfigError, RiskConfigError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "status":
        try:
            state = store.load()
        except (StateUnreadable, StateUnwritable) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"latches: {', '.join(state.latch_names()) or 'none'}")
        print(f"stops: {', '.join(sorted(state.stops)) or 'none'}")
        print(f"open exits: {', '.join(sorted(state.open_exits)) or 'none'}")
        print(f"last session: {state.last_session}")
        return 0

    actor = (args.actor or getpass.getuser()).strip()
    if not args.confirm:
        print("nothing changed: pass --confirm to reset a latch", file=sys.stderr)
        return 2
    details = {
        "latch": args.latch,
        "isin": args.isin,
        "rebase_halt_anchor": bool(args.rebase_halt_anchor),
    }

    def record(before: Any, after: Any) -> None:
        audit(
            "RISK_LATCH_RESET",
            actor,
            {**details, "latches_before": list(before.latch_names()), "latches_after": list(after.latch_names())},
            workspace="india",
        )

    try:
        store.reset(
            args.latch,
            actor,
            key=args.isin,
            rebase_halt_anchor=args.rebase_halt_anchor,
            audit=record,
        )
    except (ResetRefused, StateUnreadable, StateUnwritable) as exc:
        try:
            audit("RISK_LATCH_RESET_REFUSED", actor, {**details, "reason": str(exc)}, workspace="india")
        except Exception as audit_exc:  # noqa: BLE001 - a refusal is still a refusal
            print(f"warning: the refusal could not be audited: {audit_exc}", file=sys.stderr)
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"{args.latch} latch cleared by {actor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
