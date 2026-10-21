"""Daily-routine CLI for the relay.

    PYTHONPATH=backend uv run --project backend --no-sync python -m brokers.breeze_gateway.cli <command>

Commands: health, budget, revoke, tunnel, probe-daily, login --paste, orders-state,
orders-audit (growin-orders/1 reads: names, numbers and ids only, never a price or a quantity).

`login --paste` is the hidden-prompt fallback (D-12, OD-12): the API session code
is read with getpass, sent only as a JSON body over the IAP tunnel and never
printed, logged or written to disk. Output never holds the code, the login URL or
the relay token. Exit codes: 0 ok, 1 relay error, 2 usage or no terminal,
3 config error, 4 tunnel error.
"""

from __future__ import annotations

import argparse
import getpass
import json
import signal
import subprocess
import sys
import time
from datetime import date
from typing import Callable, Sequence, TextIO

from .client import RelayClient
from .config import ConfigError, GatewayConfig, load_gateway_config
from .errors import RelayArgumentError, RelayError, TunnelError
from .orders import OrdersClient
from .tunnel import Tunnel

EXIT_OK = 0
EXIT_RELAY = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_TUNNEL = 4

PORTAL_NOTE = "Log in through the ICICI API portal, then enter the code it shows."


def _flag(value: bool | None) -> str:
    return "unknown" if value is None else ("true" if value else "false")


def _iso(value) -> str:
    return "none" if value is None else value.isoformat()


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m brokers.breeze_gateway.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="relay state, no token needed")
    sub.add_parser("budget", help="relay call counters")
    sub.add_parser("revoke", help="end the session and forget the token")
    tunnel = sub.add_parser("tunnel", help="open the IAP tunnel for manual inspection")
    tunnel.add_argument("--verbose", action="store_true", help="show gcloud's own output")
    probe = sub.add_parser("probe-daily", help="fetch one bars window and verify its hash")
    probe.add_argument("--stock-code", required=True)
    probe.add_argument("--from", dest="from_date", required=True, type=_day)
    probe.add_argument("--to", dest="to_date", required=True, type=_day)
    sub.add_parser("orders-state", help="VM order state: kill, latches, drawdown (read only)")
    audit = sub.add_parser("orders-audit", help="VM order audit entries (read only)")
    audit.add_argument("--after-seq", type=int, default=0)
    login = sub.add_parser("login", help="log in through the relay")
    login.add_argument("--paste", action="store_true", help="type the code at a hidden prompt")
    login.add_argument("--no-open", action="store_true", help="do not open the browser")
    return parser


def _open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)  # the URL reaches only the browser, via argv


def _row_count(body: bytes) -> str:
    try:
        success = json.loads(body).get("Success")
    except (ValueError, AttributeError):
        return "unknown"
    return str(len(success)) if isinstance(success, list) else "unknown"


def _on_sigterm(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def _raise_on_sigterm() -> None:
    """Make SIGTERM unwind like Ctrl-C so `with` blocks and `finally` stop the tunnel child.

    Only the CLI entry point installs this. The library never touches the host's signals.
    """
    signal.signal(signal.SIGTERM, _on_sigterm)


def _wait_for_interrupt(handle) -> None:
    while handle.alive():
        time.sleep(1.0)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], RelayClient] | None = None,
    config_loader: Callable[[], GatewayConfig] = load_gateway_config,
    tunnel_factory: Callable[[GatewayConfig, bool], Tunnel] | None = None,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    opener: Callable[[str], None] = _open_url,
    isatty: Callable[[], bool] | None = None,
    wait: Callable[[object], None] = _wait_for_interrupt,
    out: TextIO | None = None,
) -> int:
    stream = out if out is not None else sys.stdout
    factory = client_factory if client_factory is not None else RelayClient.open

    def say(line: str) -> None:
        print(line, file=stream)

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        if args.command == "tunnel":
            make = tunnel_factory or (lambda cfg, verbose: Tunnel(cfg, stderr_passthrough=verbose))
            return _tunnel(make(config_loader(), args.verbose), say, wait)
        if args.command == "login":
            return _login(args, factory, getpass_fn, opener, isatty, say)
        with factory() as relay:
            if args.command == "health":
                health = relay.health()
                say(f"state {health.state}")
                say(f"login_pending {_flag(health.login_pending)}")
                say(f"expires_at_ist {_iso(health.expires_at_ist)}")
                say(f"egress_ok {_flag(health.egress_ok)}")
            elif args.command == "budget":
                budget = relay.budget()
                say(f"calls_last_minute {budget.calls_last_minute}")
                say(f"calls_today_ist {budget.calls_today_ist}")
                say(f"per_minute_limit {budget.per_minute_limit}")
                say(f"per_day_limit {budget.per_day_limit}")
                say(f"remaining_today {budget.remaining_today}")
                say(f"ist_date {budget.ist_date.isoformat()}")
            elif args.command == "revoke":
                say(f"state {relay.revoke().state}")
            elif args.command == "orders-state":
                order_state = OrdersClient(relay).state()
                say(f"kill {order_state.kill}")
                say(f"latches {','.join(order_state.latches) or 'none'}")
                say(f"drawdown {order_state.drawdown}")
                say(f"peak_date {order_state.peak_date or 'none'}")
                say(f"last_evaluated_session {order_state.last_evaluated_session or 'none'}")
                say(f"alerts_logged {order_state.alerts_logged}")
            elif args.command == "orders-audit":
                if args.after_seq < 0:
                    raise RelayArgumentError("after_seq")
                for entry in OrdersClient(relay).audit(args.after_seq):
                    say(
                        f"seq {entry.seq} decision {entry.decision} "
                        f"intent_id {entry.intent_id or 'none'} "
                        f"intent_sha256 {entry.intent_sha256 or 'none'} "
                        f"codes {','.join(entry.codes) or 'none'}"
                    )
            else:  # probe-daily
                payload = relay.daily_bars(args.stock_code, args.from_date, args.to_date)
                say(f"request_id {payload.request_id}")
                say(f"rows {_row_count(payload.body)}")
                say(f"empty {_flag(payload.empty)}")
                say("sha256 verified")
        return EXIT_OK
    except RelayError as err:
        # `reason` is a closed word matching [a-z_]{1,40}; it never holds a value.
        say(f"{err} reason {err.reason}" if err.reason else str(err))
        return EXIT_RELAY
    except RelayArgumentError as err:
        say(str(err))
        return EXIT_USAGE
    except ConfigError as err:
        say(f"config_error {err}")
        return EXIT_CONFIG
    except TunnelError as err:
        say(f"tunnel_error {err}")
        return EXIT_TUNNEL


def _tunnel(tunnel: Tunnel, say: Callable[[str], None], wait: Callable[[object], None]) -> int:
    handle = tunnel.start()
    try:
        say(f"RELAY_URL=http://127.0.0.1:{handle.local_port}")
        say("for manual inspection only; the relay token is not printed")
        try:
            wait(handle)
        except KeyboardInterrupt:
            return EXIT_OK
        if not handle.alive():
            say("tunnel_error tunnel_exited")
            return EXIT_TUNNEL
        return EXIT_OK
    finally:
        handle.stop()


def _login(
    args: argparse.Namespace,
    factory: Callable[[], RelayClient],
    getpass_fn: Callable[[str], str],
    opener: Callable[[str], None],
    isatty: Callable[[], bool] | None,
    say: Callable[[str], None],
) -> int:
    if not args.paste:
        say("login needs --paste in this release")
        return EXIT_USAGE
    interactive = isatty() if isatty is not None else sys.stdin.isatty()
    if not interactive:
        say("login needs an interactive terminal")  # nothing is armed without a place to type
        return EXIT_USAGE
    with factory() as relay:
        attempt = relay.login_arm()
        if args.no_open:
            say(PORTAL_NOTE)
        else:
            try:
                opener(attempt.login_url)
            except OSError:
                say("could not open a browser")
            say(PORTAL_NOTE)
        code = getpass_fn("API session code: ")
        try:
            status = relay.login_complete(attempt.attempt_id, code.strip())
        finally:
            code = ""
        say(f"state {status.state}")
        say(f"expires_at_ist {_iso(status.expires_at_ist)}")
        if relay.token_saved is False:
            say("warning: the relay token was not saved; later CLI runs will need a fresh login")
    return EXIT_OK


if __name__ == "__main__":
    _raise_on_sigterm()
    raise SystemExit(main())
