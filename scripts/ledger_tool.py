#!/usr/bin/env python3
"""Operator tool: back up, inspect, pin, verify, restore and show an execution ledger.

Run from the repository root:

    uv run --no-sync --project backend python scripts/ledger_tool.py <command> ...

Every command needs --ledger explicitly. No default path is ever inferred and
this tool reads no environment variable. Output is one JSON object on stdout.
Exit code 0 on success, 2 when a step is refused, 1 on any other error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from execution.ledger import LedgerError  # noqa: E402
from execution.ledger_migration import (  # noqa: E402
    MigrationRefused,
    apply_migration,
    inspect_ledger,
    restore_from_backup,
    show_ledger,
    verify_migration,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="back up the ledger and report workspace tags")
    inspect.add_argument("--ledger", required=True)
    inspect.add_argument("--backup-dir", required=True)

    apply = commands.add_parser("apply", help="pin a legacy ledger with explicit confirmation")
    apply.add_argument("--ledger", required=True)
    apply.add_argument("--manifest", required=True)
    # Not argparse-required: a missing confirmation is a refusal with a code.
    apply.add_argument("--confirm-workspace", choices=("uk", "india"), default=None)
    apply.add_argument("--confirm-backup-sha256", default=None)

    verify = commands.add_parser("verify", help="check the pinned ledger against the backup")
    verify.add_argument("--ledger", required=True)
    verify.add_argument("--manifest", required=True)

    restore = commands.add_parser("restore", help="move v6 files aside and restore the backup")
    restore.add_argument("--ledger", required=True)
    restore.add_argument("--manifest", required=True)
    restore.add_argument("--confirm-restore", action="store_true")

    show = commands.add_parser("show", help="read a pinned ledger without a lock")
    show.add_argument("--ledger", required=True)
    show.add_argument("--workspace", required=True, choices=("uk", "india"))
    show.add_argument("--proposal-id", default=None)
    return parser


def run(args: argparse.Namespace) -> dict:
    if args.command == "inspect":
        return inspect_ledger(args.ledger, args.backup_dir, repo_root=REPO_ROOT)
    if args.command == "apply":
        return apply_migration(
            args.ledger,
            args.manifest,
            confirm_workspace=args.confirm_workspace,
            confirm_backup_sha256_prefix=args.confirm_backup_sha256,
        )
    if args.command == "verify":
        return verify_migration(args.ledger, args.manifest)
    if args.command == "restore":
        return restore_from_backup(
            args.ledger, args.manifest, confirm_restore=args.confirm_restore
        )
    if args.command == "show":
        return show_ledger(args.ledger, args.workspace, args.proposal_id)
    raise SystemExit(f"unknown command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except MigrationRefused as exc:
        print(json.dumps({"status": "refused", "code": exc.code, "message": exc.message}))
        return 2
    except LedgerError as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1
    except Exception as exc:  # noqa: BLE001 - the operator needs one JSON object, not a trace
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
