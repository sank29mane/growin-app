"""GET /v1/orders/build: which build is this VM running (64-05 Task 2b, OI-9).

The VM answers with the commit its deploy archive was stamped with, a sha256 of every file under
its own ``orders/`` package, and one digest over both. The hashes are computed from the bytes on
disk once, when the route is mounted. Nothing is read from a file that claims a hash, and nothing
is recomputed per request. The Mac records the answer each session; the replay re-hashes a local
checkout of the same commit and refuses if the bytes differ.

The stamp lives in ``gateway_vm/BUILD_COMMIT``, outside ``orders/``: ``git archive`` expands the
placeholder in that one file (see .gitattributes), and a stamp inside ``orders/`` would make archive
bytes differ from checkout bytes. Any other deploy route keeps the placeholder, and the route then
refuses with ``build_unstamped`` rather than invent a commit.

The route has the same gate as ``GET /v1/orders/key``: bearer, then the failure gate, then an
in-memory answer. No kill read, no egress check, no budget charge, no write.

Digest recipe (three implementations, one golden test): sha256 of
``json.dumps({"commit": C, "orders": dict(sorted(M.items()))}, sort_keys=True,
separators=(",", ":"), ensure_ascii=True)`` as ASCII.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..session import RelayFailure, SessionManager
from . import CONTRACT, ROUTE_BUILD, OrderRefusal

BUILD_COMMIT_FILE = "BUILD_COMMIT"
ORDERS_DIR = "orders"
MAX_ORDERS_FILES = 64
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PATH = re.compile(r"[a-z0-9_][a-z0-9_./-]{0,95}\Z")


class BuildUnstamped(Exception):
    """The VM cannot name its build: no usable stamp, or an ``orders/`` tree the Mac would refuse."""


@dataclass(frozen=True)
class BuildIdentity:
    commit: str
    orders_sha256: Mapping[str, str]
    digest: str


def build_digest(commit: str, orders_sha256: Mapping[str, str]) -> str:
    payload = {"commit": commit, "orders": dict(sorted(orders_sha256.items()))}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _read_commit(package_dir: Path) -> str:
    try:
        text = (package_dir / BUILD_COMMIT_FILE).read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError):
        raise BuildUnstamped from None
    commit = text.strip()
    if _COMMIT.fullmatch(commit) is None:  # also refuses the unexpanded `$Format:%H$` placeholder
        raise BuildUnstamped
    return commit


def _hash_orders(package_dir: Path) -> dict[str, str]:
    """sha256 of every file under ``<package_dir>/orders``, ``__pycache__`` left out, keyed by posix path."""
    root = package_dir / ORDERS_DIR
    out: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            name = path.relative_to(root).as_posix()
            if _PATH.fullmatch(name) is None or ".." in name.split("/"):
                raise BuildUnstamped
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise BuildUnstamped from None
    if not 1 <= len(out) <= MAX_ORDERS_FILES:
        raise BuildUnstamped
    return out


def read_build_identity(package_dir: Path) -> BuildIdentity | BuildUnstamped:
    """The commit stamp, the ``orders/`` file hashes and the digest, or a ``BuildUnstamped`` instance."""
    package_dir = Path(package_dir)
    try:
        commit = _read_commit(package_dir)
        orders = _hash_orders(package_dir)
    except BuildUnstamped as err:
        return err
    return BuildIdentity(commit, orders, build_digest(commit, orders))


def _no_query(request: Request) -> None:
    if request.query_params:
        raise RelayFailure("VALIDATION", fields=["unknown"])


def mount_build_route(
    app: FastAPI,
    *,
    session: SessionManager,
    bearer: Callable[..., Any],
    package_dir: Path,
) -> None:
    """Mount ``GET /v1/orders/build``. The identity is read once, here, and closed over."""
    identity = read_build_identity(package_dir)
    body: dict[str, Any] | None = None
    if isinstance(identity, BuildIdentity):
        body = {
            "contract": CONTRACT,
            "commit": identity.commit,
            "orders_sha256": dict(identity.orders_sha256),
            "digest": identity.digest,
        }

    @app.get(ROUTE_BUILD, dependencies=[Depends(_no_query)])
    def current_build(token: str | None = Depends(bearer)):
        authorization = session.authorize(token)
        if authorization.failure:
            raise authorization.failure
        if body is None:
            raise OrderRefusal(503, "ORDERS_UNAVAILABLE", "build_unstamped")
        return JSONResponse(body)
