"""Process entry for the VM relay: `python -m gateway_vm.main --host internal-ip`.

Start-up order: arguments, relay.json, bind address (metadata), secrets
(Secret Manager), app, server. Every way that can fail ends with one
allowlisted `service_start` event on stdout and a fixed exit code, never a
traceback:

    2  refused bind address (anything but the keyword internal-ip, or a metadata
       answer that is not a private RFC 1918 IPv4 address)
    3  relay.json refused
    4  the two secrets could not be read
    1  anything unexpected

The relay listens on the VM's nic0 internal address only (OD-16). The only
environment variables it reads are CREDENTIALS_DIRECTORY and STATE_DIRECTORY,
both set by systemd. stdout carries event JSON and nothing else; uvicorn logs
to stderr at WARNING with tracebacks replaced by a fixed string.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .breeze_client import BreezeClient
from .budget import Budget
from .config import ConfigError, RelayConfig, load_relay_config
from .counter_store import CounterStore, FileCounterStore
from .egress import EgressChecker
from .events import EventLog
from .pool import PooledTransport
from .secrets import SecretManagerSource, SecretSource, SecretsUnavailable, load_credentials
from .session import SessionManager
from .transport import Clock, SystemClock, Timeouts, Transport, TransportError

HOST_KEYWORD = "internal-ip"
DEFAULT_PORT = 8443
INTERNAL_IP_URL = "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip"
_METADATA_TIMEOUTS = Timeouts(connect_s=2.0, read_s=3.0, total_s=3.0)
_RFC1918 = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

EXIT_BAD_BIND = 2
EXIT_BAD_CONFIG = 3
EXIT_NO_SECRETS = 4
EXIT_INTERNAL = 1


class NoTracebackFormatter(logging.Formatter):
    """A log record never carries a traceback or a stack: both become a fixed string."""

    def formatException(self, ei) -> str:  # noqa: N802 (logging API)
        return "[traceback suppressed]"

    def formatStack(self, stack_info: str) -> str:  # noqa: N802 (logging API)
        return "[stack suppressed]"


LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"()": NoTracebackFormatter, "format": "%(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "plain",
            "level": "WARNING",
        },
        "null": {"class": "logging.NullHandler"},
    },
    "loggers": {
        "uvicorn": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
        "uvicorn.error": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
        # The access log holds request lines and query strings: silenced entirely.
        "uvicorn.access": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
    },
}


# ------------------------------------------------------------------ wiring


def resolve_bind_address(transport: Transport) -> str:
    """The VM's nic0 internal IPv4 address from the metadata server.

    Only a private RFC 1918 dotted quad is returned. Anything else (loopback,
    link-local, unspecified, multicast, public, IPv6, a hostname, an empty or
    oversized body, an unreachable server) is a ValueError whose text names no
    value.
    """
    try:
        resp = transport.request(
            "GET",
            INTERNAL_IP_URL,
            headers={"Metadata-Flavor": "Google"},
            body=None,
            timeouts=_METADATA_TIMEOUTS,
            max_bytes=64,
        )
    except TransportError:
        raise ValueError("bind address unavailable") from None
    if resp.status != 200:
        raise ValueError("bind address unavailable")
    try:
        text = resp.body.decode("ascii").strip()
        address = ipaddress.IPv4Address(text)
    except ValueError:  # UnicodeDecodeError and AddressValueError are both ValueErrors
        raise ValueError("bind address malformed") from None
    if text.count(".") != 3 or not any(address in net for net in _RFC1918):
        raise ValueError("bind address not private")
    return str(address)


def build_app(
    config: RelayConfig,
    *,
    transport: Transport,
    secret_source: SecretSource,
    clock: Clock,
    events: EventLog,
    store: CounterStore,
) -> FastAPI:
    """Read the secrets, then wire the real relay. SecretsUnavailable propagates."""
    credentials = load_credentials(secret_source, config.secret_names)
    egress = EgressChecker(
        transport,
        expected_ip=config.expected_egress_ip,
        echo_url=config.egress_echo_url,
        clock=clock,
    )
    breeze = BreezeClient(transport, clock=clock)
    budget = Budget(
        clock,
        per_minute=config.per_minute,
        per_day=config.per_day,
        security_master_per_hour=config.security_master_per_hour,
        store=store,
    )
    session = SessionManager(
        clock=clock,
        egress=egress,
        breeze=breeze,
        credentials=credentials,
        expected_userid=config.expected_userid,
        events=events,
        budget=budget,
    )
    return create_app(
        session=session,
        breeze=breeze,
        credentials=credentials,
        budget=budget,
        events=events,
        clock=clock,
        security_master_url=config.security_master_url,
    )


def build_server(app: FastAPI, *, host: str, port: int) -> uvicorn.Server:
    """A uvicorn server that logs nothing sensitive. `host` is already validated."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
        log_config=LOG_CONFIG,
        server_header=False,
        proxy_headers=False,
        http="h11",
        loop="asyncio",
        lifespan="off",
    )
    return uvicorn.Server(config)


# --------------------------------------------------------------------- main


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # never echo what was typed
        raise _UsageError


def _parse_port(text: str) -> int:
    if not text.isascii() or not text.isdigit() or not 1024 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError("port")
    return int(text)


def _run(server: uvicorn.Server) -> None:
    server.run()


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[], Transport] = PooledTransport,
    secret_source_factory: Callable[[Transport], SecretSource] = SecretManagerSource,
    environ: Mapping[str, str] = os.environ,
    run: Callable[[uvicorn.Server], None] = _run,
    clock: Clock | None = None,
) -> int:
    clock = clock or SystemClock()
    events = EventLog(sys.stdout, clock)

    def fail(code: int, reason: str) -> int:
        events.emit("service_start", result="error", reason=reason)
        return code

    parser = _Parser(add_help=False, exit_on_error=False)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=_parse_port, default=DEFAULT_PORT)
    parser.add_argument("--config", default=None)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except (_UsageError, argparse.ArgumentError):
        return fail(EXIT_BAD_BIND, "config_invalid")
    if args.host != HOST_KEYWORD:  # a literal address is never accepted, whatever it is
        return fail(EXIT_BAD_BIND, "bind_invalid")

    directory = environ.get("CREDENTIALS_DIRECTORY")
    config_path = args.config or (str(Path(directory) / "relay.json") if directory else None)
    if config_path is None:
        return fail(EXIT_BAD_CONFIG, "config_invalid")
    try:
        config = load_relay_config(Path(config_path))
    except ConfigError:
        return fail(EXIT_BAD_CONFIG, "config_invalid")

    state_dir = environ.get("STATE_DIRECTORY")
    store = FileCounterStore(Path(state_dir) if state_dir and os.path.isdir(state_dir) else None)
    if state_dir is None or not os.path.isdir(state_dir):
        events.emit("config_alarm", result="error", reason="counter_unreadable")

    transport = transport_factory()
    try:
        try:
            host = resolve_bind_address(transport)
        except ValueError:
            return fail(EXIT_BAD_BIND, "bind_invalid")
        try:
            app = build_app(
                config,
                transport=transport,
                secret_source=secret_source_factory(transport),
                clock=clock,
                events=events,
                store=store,
            )
        except SecretsUnavailable:
            return fail(EXIT_NO_SECRETS, "secrets_unavailable")
        server = build_server(app, host=host, port=args.port)
        events.emit("service_start", result="ok")
        run(server)
        return 0
    except Exception:  # nothing unexpected may reach stderr as a traceback
        return fail(EXIT_INTERNAL, "internal")
    finally:
        close = getattr(transport, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
