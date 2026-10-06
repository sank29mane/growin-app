"""Command line entry (the only place the clock is read).

    python -m strategy_india check-gate --coverage-report PATH --run-start D --run-end D
    python -m strategy_india register   --config CONFIG.json
    python -m strategy_india run        --config CONFIG.json
    python -m strategy_india holdout    --config CONFIG.json

Exit codes: 0 done, 2 any other refusal, 3 the Phase 59 band gate refuses (D-01).
The CLI prints a JSON summary and writes nothing outside the report root it is given.
Research and backtest only: no orders, no broker or network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from .errors import GateRefused, StrategyIndiaError
from .gate import load_coverage_report


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _check_gate(args: argparse.Namespace) -> int:
    result = load_coverage_report(
        args.coverage_report, run_start=args.run_start, run_end=args.run_end, targets_sha256=args.targets_sha256,
        expected_report_sha256=args.expected_report_sha256, expected_file_sha256=args.expected_file_sha256,
    )
    _print({"gate": "open", "report_sha256": result.report_sha256, "file_sha256": result.file_sha256,
            "unavailable_bands": len(result.unavailable)})
    return 0


def _study_command(args: argparse.Namespace) -> int:
    from . import study  # imported late: pulls in numpy and scikit-learn

    config = study.load_config(args.config)
    if getattr(args, "registry_head", None):
        config["registry_head_sha256"] = args.registry_head
    if args.command == "register":
        summary = study.cli_register(config)
    elif args.command == "run":
        summary = study.cli_run(config)
    else:
        summary = study.cli_holdout(config, logged_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    _print(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strategy_india", description="India swing and momentum research engine.")
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("check-gate")
    gate.add_argument("--coverage-report", required=True, type=Path)
    gate.add_argument("--run-start", required=True, type=date.fromisoformat)
    gate.add_argument("--run-end", required=True, type=date.fromisoformat)
    gate.add_argument("--targets-sha256", required=True, help="59 TargetUniverseResult.target_sha256")
    gate.add_argument("--expected-report-sha256")
    gate.add_argument("--expected-file-sha256")
    for name in ("register", "run", "holdout"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", required=True, type=Path)
        cmd.add_argument("--registry-head", help="the pinned registry head sha256 (overrides or confirms holdout_refs)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check-gate":
            return _check_gate(args)
        return _study_command(args)
    except GateRefused as exc:
        _print({"refused": exc.code, "error": str(exc)})
        return exc.exit_code
    except StrategyIndiaError as exc:
        _print({"refused": exc.code, "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
