"""Operator commands for the shadow runner (64-03 Task 3): ``python -m shadow_india <command>``.

``resume`` and ``status`` read and write only the evidence of one run. ``open-run``, ``decide``, ``close`` and
``hand-flatten`` need a runtime (relay link, session store, calendar, reference prices) that this module does not build:
the production runtime is deliberately not wired here, so run as shipped they refuse with ``production_runtime_not_wired``
and touch nothing. A test or the operator's own wrapper passes ``runtime_factory`` to ``main``.

Every command that opens the evidence store (``resume``, ``status``) opens it with the registered forbidden literals
(``_forbidden_literals``): an operator-typed ``--reason`` is the one free-text field that reaches a hash-chained record, and
a literal that slips past the pattern layer is kept forever. With no registered source the command refuses
(``forbidden_literals_not_registered``) and opens nothing. The future production runtime must call the same helper for
its own ``EvidenceStore.open`` and ``EvidenceStore.create`` and pass the result as ``forbidden=``.

No command authorizes, signs or sends an order. There is no ``release-halt`` here: a halt is released through the
portfolio's latch-backed path, never through ``resume``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from typing import Any, Callable, Collection, Optional, Sequence, TextIO

from brokers.breeze_gateway.errors import RelayError

from private_config.errors import PrivateConfigError
from private_config.loader import load_shadow_forbidden_literals

from . import policy, vm_codes
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
NO_FORBIDDEN_SOURCE = "forbidden_literals_not_registered"


class RuntimeNotWired(Exception):
    pass


class ForbiddenLiteralsRefused(Exception):
    """No usable registered forbidden literals: the evidence store is not opened."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        if name in ("resume", "status"):
            command.add_argument(
                "--private-dir", default=None,
                help="private/ directory holding india/shadow_forbidden.json (the registered forbidden literals). "
                     "Required: resume and status refuse without it (forbidden_literals_not_registered)",
            )
        if name in ("resume", "hand-flatten"):
            command.add_argument("--reason", required=True)
        if name == "hand-flatten":
            command.add_argument(
                "--include-corporate-action", action="store_true",
                help="also sell names with an open unresolved_corporate_action pause (default: leave them paused)",
            )
        if name == "resume":
            command.add_argument(
                "--pause-seq", type=int, action="append", dest="pause_seq", metavar="N",
                help="clear only this open pause (repeatable); without it every pause `resume` may clear is cleared",
            )
    return parser


# An open pause ``policy.resume`` refuses to clear, and the typed refusal it would raise. Without ``--pause-seq`` the
# CLI clears what it can and names these, so one halt cannot keep a relay pause open forever.
_NOT_RESUMABLE = {
    policy.DRAWDOWN_HALT: "drawdown_resume_requires_latch",
    policy.UNRESOLVED_CORPORATE_ACTION: "corporate_action_resume_requires_portfolio",
}


def _resume(
    store: EvidenceStore, reason: str, pause_seqs: Optional[Sequence[int]], ambiguous_isins: Optional[Collection[str]] = None
) -> dict[str, Any]:
    """``ambiguous_isins`` is the portfolio's still-ambiguous set; ``None`` (no runtime) keeps an ambiguous pause open."""

    if pause_seqs:
        record = policy.resume(  # a named pause is cleared or refused, never skipped
            store, reason, pause_seqs=pause_seqs, ambiguous_isins=ambiguous_isins
        )
        return {"resumed": record["cleared_pause_seqs"], "seq": record["seq"], "skipped": []}
    state = policy.run_state(store)

    def why(pause: policy.OpenPause) -> Optional[str]:
        return _NOT_RESUMABLE.get(pause.event) or policy.ambiguous_exit_refusal(pause, ambiguous_isins)

    clearable = [p.seq for p in state.open_pauses if why(p) is None]
    skipped = [{"seq": p.seq, "event": p.event, "why": why(p)} for p in state.open_pauses if why(p) is not None]
    if not clearable:
        if skipped and not state.aborted and isinstance(reason, str) and reason.strip():
            raise policy.PolicyError(str(skipped[0]["why"]))
        record = policy.resume(store, reason)  # the typed refusal: abort, blank reason, run end, nothing open
        return {"resumed": record["cleared_pause_seqs"], "seq": record["seq"], "skipped": []}
    record = policy.resume(store, reason, pause_seqs=clearable, ambiguous_isins=ambiguous_isins)
    return {"resumed": record["cleared_pause_seqs"], "seq": record["seq"], "skipped": skipped}


def _ambiguous_isins(
    args: argparse.Namespace, store: EvidenceStore, runtime_factory: Optional[Callable[[argparse.Namespace], Any]]
) -> Optional[frozenset[str]]:
    """The portfolio's still-ambiguous exit names, asked only when an ``ambiguous_exit_fill`` pause is open.

    No wired runtime means nobody can say the fill was resolved: ``None``, and that pause stays open."""

    if not any(p.event == policy.AMBIGUOUS_EXIT_FILL for p in policy.run_state(store).open_pauses):
        return frozenset()
    try:
        runtime = (runtime_factory or _default_runtime)(args)
    except RuntimeNotWired:
        return None
    runner: ShadowRunner = runtime.runner if hasattr(runtime, "runner") else runtime
    return runner.ambiguous_exit_isins()


def _default_forbidden_literals(args: argparse.Namespace) -> Sequence[str]:
    """The registered literals from ``<private-dir>/india/shadow_forbidden.json`` through the fail-closed loader."""

    private_dir = getattr(args, "private_dir", None)
    if private_dir is None:
        raise ForbiddenLiteralsRefused(NO_FORBIDDEN_SOURCE)
    try:
        return load_shadow_forbidden_literals(private_dir)
    except PrivateConfigError as error:
        raise ForbiddenLiteralsRefused(f"forbidden_literals_refused:{error.code}") from None


def _forbidden_literals(
    args: argparse.Namespace, source: Optional[Callable[[argparse.Namespace], Sequence[str]]] = None
) -> tuple[str, ...]:
    """The literals every CLI ``EvidenceStore.open`` passes as ``forbidden=``. Never empty: an empty list is a refusal.

    ``source`` is the explicit seam for a test or the operator's own wrapper, like ``runtime_factory``. It does not
    bypass the empty check."""

    literals = tuple((source or _default_forbidden_literals)(args))
    if not literals or not all(isinstance(item, str) and item for item in literals):
        raise ForbiddenLiteralsRefused("forbidden_literals_empty")
    return literals


def _print(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _default_runtime(args: argparse.Namespace) -> Any:
    raise RuntimeNotWired(NOT_WIRED)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime_factory: Optional[Callable[[argparse.Namespace], Any]] = None,
    forbidden_source: Optional[Callable[[argparse.Namespace], Sequence[str]]] = None,
    out: Optional[TextIO] = None,
) -> int:
    out = out or sys.stdout
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.command in ("resume", "status"):
            store = EvidenceStore.open(
                args.root, run_id=args.run_id, session_id=session_id_for(args.session),
                forbidden=_forbidden_literals(args, forbidden_source),
            )
            if args.command == "status":
                state = policy.run_state(store)
                seq, head = store.head()
                _print(out, {
                    "aborted": list(state.abort_events), "ended": list(state.ended_events),
                    "open_pauses": [{"seq": p.seq, "event": p.event, "scope": p.scope} for p in state.open_pauses],
                    "records": seq, "head": head,
                })
                return EXIT_OK
            _print(out, _resume(store, args.reason, args.pause_seq, _ambiguous_isins(args, store, runtime_factory)))
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
            report = runner.hand_flatten(
                args.session, args.reason, include_corporate_action=args.include_corporate_action
            )
            _print(out, {"status": report.status, "stopped": report.stopped, "intents": report.intents,
                         "skipped": report.skipped})
            return EXIT_ABORTED if report.status == "aborted" else EXIT_OK
        if args.command == "open-run":
            _print(out, {"opened": runtime.open_run()})
            return EXIT_OK
    except RuntimeNotWired as exc:
        _print(out, {"refused": str(exc)})
        return EXIT_REFUSED
    except ForbiddenLiteralsRefused as exc:
        _print(out, {"refused": exc.code})
        return EXIT_REFUSED
    except (DecideRefused, OpenRunRefused, policy.PolicyError) as exc:
        _print(out, {"refused": exc.code})
        return EXIT_ABORTED if exc.code == "run_aborted" else EXIT_REFUSED
    except policy.RunAborted:
        _print(out, {"refused": "run_aborted"})
        return EXIT_ABORTED
    except RelayError as error:  # a relay failure nothing recorded (the runner pauses on its own reads): typed, never a traceback
        verdict = vm_codes.classify_error(error)
        _print(out, {"refused": "relay_error", "event": verdict.event, "code": verdict.code, "status": verdict.status})
        return EXIT_ABORTED if verdict.kind == vm_codes.ABORT else EXIT_REFUSED
    except EvidenceCorrupt as exc:
        _print(out, {"aborted": exc.code})
        return EXIT_ABORTED
    except EvidenceError as exc:
        _print(out, {"refused": exc.code})
        return EXIT_REFUSED
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
