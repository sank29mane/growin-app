"""Operator commands for the shadow runner (64-03 Task 3): ``python -m shadow_india <command>``.

``resume`` and ``status`` read and write only the evidence of one run. ``open-run``, ``decide``, ``close`` and
``hand-flatten`` need a runtime (relay link, session store, calendar, reference prices) that this module does not build:
the production runtime is deliberately not wired here, so run as shipped they refuse with ``production_runtime_not_wired``
and touch nothing. A test or the operator's own wrapper passes ``runtime_factory`` to ``main``.

No command authorizes, signs or sends an order. There is no ``release-halt`` here: a halt is released through the
portfolio's latch-backed path, never through ``resume``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any, Callable, Optional, Sequence, TextIO

from . import policy
from .evidence import EvidenceCorrupt, EvidenceError, EvidenceStore
from .runner import (
    DecideRefused,
    OpenRunRefused,
    ShadowRunner,
    session_id_for,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_ABORTED = 3
NOT_WIRED = "production_runtime_not_wired"


class RuntimeNotWired(Exception):
    pass


def _day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shadow_india", description="Shadow runner operator commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("open-run", "decide", "close", "resume", "hand-flatten", "status"):
        command = sub.add_parser(name)
        command.add_argument("--root", required=True, help="evidence root (outside the repository)")
        command.add_argument("--run-id", required=True)
        command.add_argument("--session", required=True, type=_day, help="session date, YYYY-MM-DD")
        if name in ("resume", "hand-flatten"):
            command.add_argument("--reason", required=True)
    return parser


def _print(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _default_runtime(args: argparse.Namespace) -> Any:
    raise RuntimeNotWired(NOT_WIRED)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime_factory: Optional[Callable[[argparse.Namespace], Any]] = None,
    out: Optional[TextIO] = None,
) -> int:
    out = out or sys.stdout
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.command in ("resume", "status"):
            store = EvidenceStore.open(args.root, run_id=args.run_id, session_id=session_id_for(args.session))
            if args.command == "status":
                state = policy.run_state(store)
                seq, head = store.head()
                _print(out, {
                    "aborted": list(state.abort_events), "ended": list(state.ended_events),
                    "open_pauses": [{"seq": p.seq, "event": p.event, "scope": p.scope} for p in state.open_pauses],
                    "records": seq, "head": head,
                })
                return EXIT_OK
            record = policy.resume(store, args.reason)
            _print(out, {"resumed": record["cleared_pause_seqs"], "seq": record["seq"]})
            return EXIT_OK
        runtime = (runtime_factory or _default_runtime)(args)
        runner: ShadowRunner = runtime.runner if hasattr(runtime, "runner") else runtime
        if args.command == "decide":
            report = runner.decide(args.session)
            _print(out, {"status": report.status, "stopped": report.stopped, "intents": report.intents,
                         "skipped": report.skipped, "marked": report.marked})
            return EXIT_ABORTED if report.status == "aborted" else EXIT_OK
        if args.command == "close":
            closed = runner.close(args.session)
            _print(out, {"outcome": closed.outcome, "released": closed.released, "witness": closed.witness})
            return EXIT_ABORTED if closed.outcome == "aborted" else EXIT_OK
        if args.command == "hand-flatten":
            report = runner.hand_flatten(args.session, args.reason)
            _print(out, {"status": report.status, "intents": report.intents, "skipped": report.skipped})
            return EXIT_ABORTED if report.status == "aborted" else EXIT_OK
        if args.command == "open-run":
            _print(out, {"opened": runtime.open_run()})
            return EXIT_OK
    except RuntimeNotWired as exc:
        _print(out, {"refused": str(exc)})
        return EXIT_REFUSED
    except (DecideRefused, OpenRunRefused, policy.PolicyError) as exc:
        _print(out, {"refused": exc.code})
        return EXIT_ABORTED if exc.code == "run_aborted" else EXIT_REFUSED
    except policy.RunAborted:
        _print(out, {"refused": "run_aborted"})
        return EXIT_ABORTED
    except EvidenceCorrupt as exc:
        _print(out, {"aborted": exc.code})
        return EXIT_ABORTED
    except EvidenceError as exc:
        _print(out, {"refused": exc.code})
        return EXIT_REFUSED
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
