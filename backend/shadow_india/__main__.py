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

No command authorizes, signs or sends an order. ``resume`` never clears a halt or a corporate-action pause: they are
released only by ``release-halt`` and ``release-corporate-action`` (64-09 Task A), which call nothing but
``ShadowPortfolio.release_halt`` and ``ShadowPortfolio.release_corporate_action``. Before the portfolio is built they
screen the reason and the actor (grammar, registered literals, the evidence pattern layer: ``release_halt`` resets the
latch, which stores the actor, before its evidence append) and refuse from read-only reads of the evidence and the latch
file, with no runtime factory call. The production latch path is not wired until 64-09 Task B, so as shipped both
commands refuse ``production_runtime_not_wired``; a test or the operator's wrapper passes ``latch_path_source`` and
``runtime_factory``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from typing import Any, Callable, Collection, Optional, Sequence, TextIO

from brokers.breeze_gateway.errors import RelayError

from market_data.models import is_valid_isin
from private_config.errors import PrivateConfigError
from private_config.loader import load_shadow_forbidden_literals
from risk_india.state import StateUnreadable, read_state

from . import policy, vm_codes
from .evidence import EvidenceCorrupt, EvidenceError, EvidenceStore, read_records, validate_payload
from .portfolio import PortfolioAborted, PortfolioError, ShadowPortfolio
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
RELEASE_COMMANDS = ("release-halt", "release-corporate-action")
# An operator name written to the latch: lowercase ASCII, short. ``\Z`` so a trailing newline does not pass.
_ACTOR = re.compile(r"[a-z][a-z0-9_.-]{0,31}\Z")


class RuntimeNotWired(Exception):
    pass


class ReleaseRefused(Exception):
    """A release command refused from its arguments or a read-only read, before any runtime is built."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    for name in ("open-run", "decide", "close", "resume", "hand-flatten", "status", *RELEASE_COMMANDS):
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
        if name in RELEASE_COMMANDS:
            command.add_argument(
                "--private-dir", required=True,
                help="private/ directory holding india/shadow_forbidden.json (the registered forbidden literals)",
            )
            command.add_argument("--reason", required=True, help="why the pause is released (screened, then recorded)")
        if name == "release-halt":
            command.add_argument("--actor", required=True, help="who releases it: [a-z][a-z0-9_.-]{0,31}")
            command.add_argument(
                "--rebase-halt-anchor", action="store_true",
                help="accept the loss and restart only the -8% test (the peak never rebases); without it a release "
                     "at or below the halt line is refused halt_release_refused",
            )
        if name == "release-corporate-action":
            command.add_argument("--isin", required=True, help="the held name whose unresolved_corporate_action pause to release")
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
# CLI clears what it can and names these, so one halt cannot keep a relay pause open forever. The release is
# ``release-halt`` for the first and ``release-corporate-action --isin`` for the second, never ``resume``.
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


def _default_latch_path(args: argparse.Namespace) -> Any:
    """The production latch path is wired with the production runtime (64-09 Task B): until then, refuse."""

    raise RuntimeNotWired(NOT_WIRED)


def _screen_release_text(text: str, literals: Sequence[str]) -> None:
    """The evidence pattern layer and the registered literals over one free-text value, with no side effect.

    ``release_halt`` resets the latch before it appends its evidence record, and the latch stores the actor: a value
    only the append would refuse would already be on disk. So both values are screened here, ``resume``-shaped."""

    validate_payload("resume", {"reason": text, "cleared_pause_seqs": []}, forbidden=literals)


def _release_checks(args: argparse.Namespace, literals: Sequence[str]) -> str:
    """Steps 2 and 3 of a release: grammar, then the pre-screen. Returns the stripped reason. Reads and writes nothing."""

    reason = args.reason.strip() if isinstance(args.reason, str) else ""
    if not reason:
        raise ReleaseRefused("reason_required")
    if args.command == "release-corporate-action" and not is_valid_isin(args.isin):
        raise ReleaseRefused("isin_invalid")
    if args.command == "release-halt" and not _ACTOR.match(args.actor):
        raise ReleaseRefused("actor_invalid")
    _screen_release_text(reason, literals)
    if args.command == "release-halt":
        _screen_release_text(args.actor, literals)
    return reason


def _release_read_only_state(
    args: argparse.Namespace, latch_path_source: Optional[Callable[[argparse.Namespace], Any]]
) -> bool:
    """Step 4: refuse from read-only reads, before any runtime factory runs. Returns whether the latch holds a halt.

    No sidecar, no start state and no lock file is written: ``read_records`` and ``read_state`` only read. An
    unreadable latch is passed on, so the portfolio records ``state_corrupt`` for it."""

    latch_path = (latch_path_source or _default_latch_path)(args)
    run = policy.state_from_records(read_records(args.root, args.run_id))
    if run.aborted:
        raise policy.PolicyError("resume_refused_abort")
    releasing_halt = args.command == "release-halt"
    if releasing_halt and run.ended:
        raise policy.PolicyError("resume_refused_run_ended")
    open_pause = any(p.event == policy.DRAWDOWN_HALT for p in run.open_pauses)
    isin_scope = None if releasing_halt else f"{policy.SCOPE_ISIN}:{args.isin}"
    open_action = any(
        p.event == policy.UNRESOLVED_CORPORATE_ACTION and p.scope == isin_scope for p in run.open_pauses
    )
    try:
        latch = read_state(latch_path)
    except StateUnreadable:
        if not releasing_halt and not open_action:
            raise policy.PolicyError("nothing_to_resume") from None
        return False
    if latch is None:
        raise ReleaseRefused("latch_missing")
    latched = "halt" in latch.latch_names()  # the flag by name: the runner's source scan bans the bare attribute
    if releasing_halt:
        if latch.ended:
            raise policy.PolicyError("resume_refused_run_ended")
        if not latched and not open_pause:
            raise policy.PolicyError("nothing_to_resume")
    elif not open_action:
        raise policy.PolicyError("nothing_to_resume")
    return latched


def _release(
    args: argparse.Namespace,
    runtime_factory: Optional[Callable[[argparse.Namespace], Any]],
    latch_path_source: Optional[Callable[[argparse.Namespace], Any]],
    forbidden_source: Optional[Callable[[argparse.Namespace], Sequence[str]]],
) -> dict[str, Any]:
    literals = _forbidden_literals(args, forbidden_source)  # 1
    reason = _release_checks(args, literals)  # 2, 3
    latch_halted = _release_read_only_state(args, latch_path_source)  # 4
    runtime = (runtime_factory or _default_runtime)(args)  # 5
    portfolio = getattr(runtime, "portfolio", None)
    if portfolio is None:
        portfolio = getattr(getattr(runtime, "runner", runtime), "portfolio", None)
    if not isinstance(portfolio, ShadowPortfolio):  # 6: a duck-typed stand-in could clear anything
        raise ReleaseRefused("release_requires_portfolio")
    if args.command == "release-halt":  # 7
        record = portfolio.release_halt(args.actor, reason, rebase_halt_anchor=args.rebase_halt_anchor)
        kind, rebased = "halt", bool(args.rebase_halt_anchor and latch_halted)
    else:
        record = portfolio.release_corporate_action(args.isin, reason)
        kind, rebased = "corporate_action", False
    return {
        "released": kind, "cleared_pause_seqs": list(record.get("cleared_pause_seqs", [])), "seq": record.get("seq"),
        "rebased": rebased,
    }


def _print(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _default_runtime(args: argparse.Namespace) -> Any:
    raise RuntimeNotWired(NOT_WIRED)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime_factory: Optional[Callable[[argparse.Namespace], Any]] = None,
    forbidden_source: Optional[Callable[[argparse.Namespace], Sequence[str]]] = None,
    latch_path_source: Optional[Callable[[argparse.Namespace], Any]] = None,
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
        if args.command in RELEASE_COMMANDS:
            _print(out, _release(args, runtime_factory, latch_path_source, forbidden_source))
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
    except PortfolioAborted as exc:  # the portfolio recorded an abort event: terminal, exit 3
        _print(out, {"aborted": exc.code})
        return EXIT_ABORTED
    except PortfolioError as exc:
        _print(out, {"refused": exc.code})
        return EXIT_REFUSED
    except (DecideRefused, OpenRunRefused, ReleaseRefused, policy.PolicyError) as exc:
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
