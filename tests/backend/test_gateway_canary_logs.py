"""GATE-03 canary: credential-shaped values never leave the places designed for them.

Random canary values (AppKey, secret key, API_Session code, Breeze session token, user id) go
through the real VM app (main.build_app over a fake network), the real Mac RelayClient and
OrdersClient, the real CLI `login --paste` path and the real FileTokenStore. Everything the run
can emit is captured and searched for each value in several encodings: log records, stdout and
stderr, the VM event sink, every response body and header the client receives, exception text,
the CLI output stream and every file under the watched root.

Files are inspected while the run happens, not only after it (the run ends with `revoke`, which
deletes the token file, so an after-run scan alone would miss any write-then-delete):

  layer 1, an audit hook (`HookCore`): every byte about to be deleted, truncated or replaced
           under a watched root is read at that moment and scanned. `file.truncate()` fires no
           audit event on CPython 3.11, so a profile hook (`sys.setprofile`, which sees the C call
           to `truncate` and the file object it is bound to) flushes and reads the file first.
           Code that removes that hook cannot be out-raced, so it is checked instead: at every
           stage boundary and at exit `sys.getprofile()` and `threading.getprofile()` must still
           be the recorder's hook, a thread must start and run under it, and a Python child
           checks at its exit; anything else is `profile-replaced`, a failure;
  layer 2, stage snapshots: every regular file under the watched root is read at each stage
           boundary, and once more immediately before `revoke` (the `pre-revoke` stage).

Python children started during a run carry the same hooks through a generated `sitecustomize`.
A Python child started so that it skips `sitecustomize` (-I, -E, -S) fails the run
(`child-unwatched`), and so does `os.fork` / `os.forkpty` (a forked child keeps its records in its
own memory and loses them at exit). A thread whose `run` is overridden in a subclass is checked at
start only, not around `run`. A file larger than `HookCore.MAX_READ` fails the run
(`file-too-large`) instead of being cut off.

Residuals, both reported in the PR:
  RS-1  native or non-Python children, and native code that writes without Python's os or io
        calls, are out of scope for same-stage write-then-delete detection; layer 2 still
        catches what they leave at a stage boundary.
  RS-2  canary bytes overwritten in place through the same handle (seek or `os.lseek`/`os.pwrite`,
        then write, or an mmap store) and then deleted: no audit event or C call names the old
        bytes. Bytes already in a file when it is opened for writing (truncating or not) are read
        at the open, so reopening to overwrite is covered.

Every detector here is shown able to fail: a planted leak, a planted write-then-delete (several
shapes) and a write the hook cannot see are each reported. Only canary values appear in this
file. The vendor SDK's name is never written here (the 61-01 scan rejects it).
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence
from unittest.mock import patch
from urllib.parse import quote, quote_plus, urlsplit

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from brokers.breeze_gateway import cli as cli_mod  # noqa: E402
from brokers.breeze_gateway.client import RelayClient  # noqa: E402
from brokers.breeze_gateway.errors import RelayError  # noqa: E402
from brokers.breeze_gateway.orders import OrdersClient  # noqa: E402
from brokers.breeze_gateway.token_store import FileTokenStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from gateway_vm import contract  # noqa: E402
from gateway_vm.config import RelayConfig  # noqa: E402
from gateway_vm.counter_store import CounterUnreadable, FileCounterStore  # noqa: E402
from gateway_vm.events import EventLog  # noqa: E402
from gateway_vm.main import build_app  # noqa: E402
from gateway_vm.orders import CONTRACT as ORDERS_CONTRACT  # noqa: E402
from gateway_vm.orders.kill import KILL_PATH  # noqa: E402
from gateway_vm.orders.routes import OrderSettings  # noqa: E402
from gateway_vm.session import SessionManager  # noqa: E402
from gateway_vm.transport import HttpResponse  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
PRIMARY = VECTORS["keys"]["primary"]

BASE = "http://127.0.0.1:8443"
EXPECTED_IP = "203.0.113.7"
T0 = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)  # 10:00 IST on a trading day

TOKEN_REL = "home/.config/growin/relay-session.json"
TOKEN_TEMP_RE = re.compile(r"^home/\.config/growin/\.relay-session\.json\.\d+\.[0-9a-f]{8}\.tmp$")
SURFACE_FILE_RE = re.compile(r"^file(?:-lost)?:(?P<rel>.+?)@(?P<stage>[^@ ]+)(?: \(child\))?$")

PROBLEM_KINDS = ("child-unwatched", "file-unresolved", "file-too-large", "hook-error", "profile-replaced")


# =============================================================== the hook core
#
# One class, two homes: it is used in this process, and its source is written verbatim into the
# `sitecustomize` that Python children load. Keep it self-contained: only os, sys, stat, re,
# threading and fcntl (imported where used) may appear in it.


class HookCore:
    """An audit hook that reads bytes about to be lost under watched roots, and checks children."""

    HANDLED = (
        "open",
        "os.remove",
        "os.rename",
        "os.truncate",
        "os.rmdir",
        "shutil.rmtree",
        "subprocess.Popen",
        "os.posix_spawn",
        "os.fork",
        "os.forkpty",
    )
    PROFILED = ("file.truncate",)
    CHUNK = 1024 * 1024
    MAX_READ = 64 * 1024 * 1024  # past this the run fails (`file-too-large`); never a silent cut
    PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)*)?$")
    VALUE_OPTIONS = re.compile(r"[cmWX]")

    def __init__(self, roots, ignored, emit, stage, active, hook_dir, python_real, track_seen=True):
        self.roots = [os.path.realpath(str(root)) for root in roots]
        self.ignored = [os.path.realpath(str(path)) for path in ignored]
        self.hook_dir = os.path.realpath(str(hook_dir))
        self.python_real = python_real
        self._emit_cb = emit
        self._stage = stage
        self._active = active
        self._track_seen = track_seen
        self._tls = threading.local()

    def __call__(self, event, args):
        if event not in self.HANDLED or getattr(self._tls, "busy", False) or not self._active():
            return
        self._tls.busy = True  # the hook's own reads must not recurse into the hook
        try:
            getattr(self, "on_" + event.replace(".", "_"))(*args)
        except Exception as exc:  # never break the audited call; the run fails instead
            self._emit("hook-error", f"{event}:{type(exc).__name__}")
        finally:
            self._tls.busy = False

    # ---------------------------------------------------------------- helpers

    def _emit(self, kind, name, data=b""):
        if kind == "seen" and not self._track_seen:
            return
        self._emit_cb(kind, name, self._stage(), data)

    def _norm(self, raw):
        head, tail = os.path.split(os.path.abspath(raw))
        return os.path.join(os.path.realpath(head), tail)

    def _fd_path(self, fd):
        try:
            return os.path.realpath(os.readlink(f"/proc/self/fd/{int(fd)}"))
        except (OSError, ValueError):
            pass
        try:
            import fcntl

            raw = fcntl.fcntl(int(fd), getattr(fcntl, "F_GETPATH", 50), b"\0" * 1024)
            return os.path.realpath(raw.split(b"\0", 1)[0].decode())
        except (OSError, ImportError, ValueError, TypeError):
            return None

    def _path(self, target, dir_fd=None):
        """An absolute, normalised path, or None (recorded as file-unresolved when unknowable)."""
        if isinstance(target, int):
            path = self._fd_path(target)
            if path is None:
                self._emit("file-unresolved", f"fd{target}")
            return path
        try:
            raw = os.fsdecode(target)
        except (TypeError, ValueError):
            return None
        if not os.path.isabs(raw) and dir_fd not in (None, -1):
            base = self._fd_path(dir_fd)
            if base is None:
                self._emit("file-unresolved", os.path.basename(raw))
                return None
            raw = os.path.join(base, raw)
        return self._norm(raw)

    def _locate(self, path):
        for ignored in self.ignored:
            if path == ignored or path.startswith(ignored + os.sep):
                return None
        for root in self.roots:
            if path == root or path.startswith(root + os.sep):
                return root, os.path.relpath(path, root)
        return None

    def _read(self, path, rel):
        """The whole file, read in bounded chunks and joined, so a value that straddles a chunk
        boundary is still contiguous. A file past MAX_READ is a failure, not a short read."""
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        chunks, size = [], 0
        try:
            while True:
                chunk = os.read(fd, self.CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self.MAX_READ:
                    self._emit("file-too-large", rel)
                    break
        except OSError:
            self._emit("hook-error", "read:" + os.path.basename(path))
        finally:
            os.close(fd)
        return b"".join(chunks)

    def _lose(self, path):
        """Read every byte about to be lost at `path` (a file, or every file under a directory)."""
        located = self._locate(path)
        if located is None:
            return
        root, rel = located
        if os.path.isdir(path) and not os.path.islink(path):
            for dirpath, _dirs, files in os.walk(path):
                for name in files:
                    full = os.path.join(dirpath, name)
                    data = self._read(full, os.path.relpath(full, root))
                    if data is not None:
                        self._emit("file-lost", os.path.relpath(full, root), data)
            return
        data = self._read(path, rel)
        if data is not None:
            self._emit("file-lost", rel, data)

    def _touch(self, event, target, dir_fd=None, lose=True):
        path = self._path(target, dir_fd)
        if path is None:
            return
        located = self._locate(path)
        if located is None:
            return
        self._emit("seen", f"{event} {located[1]}")
        if lose:
            self._lose(path)

    # ---------------------------------------------------------------- handlers

    def on_open(self, target, mode=None, flags=0):
        if isinstance(target, int):
            return
        flags = flags if isinstance(flags, int) else 0
        mode = mode if isinstance(mode, str) else ""
        writing = bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC)
        ) or any(char in mode for char in "wax+")
        if not writing:
            return
        self._touch("open", target)  # whatever the file holds now (truncating or not) is read first

    def on_os_remove(self, target, dir_fd=None):
        self._touch("os.remove", target, dir_fd)

    def on_os_rename(self, src, dst, src_dir_fd=None, dst_dir_fd=None):
        self._touch("os.rename", src, src_dir_fd)
        self._touch("os.rename", dst, dst_dir_fd)

    def on_os_truncate(self, target, length=None):
        self._touch("os.truncate", target)

    def on_os_rmdir(self, target, dir_fd=None):
        self._touch("os.rmdir", target, dir_fd)

    def on_shutil_rmtree(self, target, dir_fd=None):
        self._touch("shutil.rmtree", target, dir_fd)

    def on_subprocess_Popen(self, executable, args=None, cwd=None, env=None):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        self._emit("seen", "subprocess.Popen")
        self._child(executable, argv, env)

    def on_os_posix_spawn(self, path, argv=None, env=None):
        self._emit("seen", "os.posix_spawn")
        self._child(path, list(argv or []), env)

    def on_os_fork(self):
        # A forked child inherits this hook but appends to its own copy of the records, which
        # die with it. Fail closed in the parent, where the record survives.
        self._emit("seen", "os.fork")
        self._emit("child-unwatched", "os.fork")

    def on_os_forkpty(self):
        self._emit("seen", "os.forkpty")
        self._emit("child-unwatched", "os.forkpty")

    # ---------------------------------------------------------------- profile hook

    def install_profile(self):
        sys.setprofile(self.profile)
        threading.setprofile(self.profile)  # threads started from now on

    def check_hook(self, where, current):
        """Fail closed: a hook that is no longer ours means truncates can go unseen."""
        if self._active() and current != self.profile:
            self._emit("profile-replaced", where)

    def check_profile(self):
        self.check_hook("sys", sys.getprofile())
        self.check_hook("threading", threading.getprofile())

    def profile(self, frame, event, arg):
        """`file.truncate()` raises no audit event: read the file at the C call, before it runs."""
        if event != "c_call" or getattr(arg, "__name__", "") != "truncate":
            return
        target = getattr(arg, "__self__", None)
        if target is None or isinstance(target, type(sys)):  # os.truncate has its own audit event
            return
        if getattr(self._tls, "busy", False) or not self._active():
            return
        self._tls.busy = True
        try:
            try:
                fd = target.fileno()
            except (OSError, ValueError, AttributeError):
                return  # not backed by a descriptor (BytesIO, StringIO): nothing on disk to lose
            try:
                target.flush()  # bytes still in Python's buffer reach the file, as truncate will do
            except (OSError, ValueError):
                pass
            path = self._fd_path(fd)
            if path is None:
                self._emit("file-unresolved", f"fd{fd}")
                return
            located = self._locate(path)
            if located is None:
                return
            self._emit("seen", "file.truncate " + located[1])
            self._lose(path)
        except Exception as exc:
            self._emit("hook-error", "file.truncate:" + type(exc).__name__)
        finally:
            self._tls.busy = False

    # ---------------------------------------------------------------- children

    def _env_value(self, environ, key):
        for candidate in (key, key.encode()):
            try:
                value = environ.get(candidate)
            except Exception:
                value = None
            if value is not None:
                return os.fsdecode(value)
        return ""

    def _is_python(self, executable):
        try:
            text = os.fsdecode(executable)
        except (TypeError, ValueError):
            return False
        if self.PYTHON_NAME.match(os.path.basename(text)):
            return True
        return os.path.realpath(text) == self.python_real

    def _skips_site(self, argv):
        """True when the interpreter flags (-I, -E, -S) mean sitecustomize or PYTHONPATH is skipped."""
        for arg in argv[1:]:
            try:
                text = os.fsdecode(arg)
            except (TypeError, ValueError):
                break
            if not text.startswith("-") or text in ("-c", "-m"):
                break
            if text.startswith("--"):
                continue
            if set(self.VALUE_OPTIONS.split(text[1:], 1)[0]) & set("IES"):
                return True
        return False

    def _child(self, executable, argv, env):
        try:
            name = os.path.basename(os.fsdecode(executable))
        except (TypeError, ValueError):
            name = "?"
        if not self._is_python(executable):
            self._emit("child-native", name)
            return
        environ = env if env is not None else os.environ
        pythonpath = self._env_value(environ, "PYTHONPATH")
        on_path = any(
            os.path.realpath(entry) == self.hook_dir for entry in pythonpath.split(os.pathsep) if entry
        )
        if self._skips_site(argv) or not on_path or not self._env_value(environ, "GROWIN_CANARY_HOOK"):
            self._emit("child-unwatched", name)


_CHILD_HEADER = "from __future__ import annotations\n\nimport base64, json, os, re, stat, sys, threading\n\n\n"

_CHILD_FOOTER = r'''


def _growin_install():
    cfg = json.loads(os.environ["GROWIN_CANARY_HOOK"])
    capture = cfg["capture"]
    stage_file = os.path.join(capture, "stage")
    out = os.path.join(capture, "child-%d-%s.jsonl" % (os.getpid(), os.urandom(4).hex()))

    def emit(kind, name, stage, data):
        line = json.dumps(
            {"kind": kind, "name": name, "stage": stage, "data": base64.b64encode(data).decode()}
        )
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)

    def stage():
        try:
            with open(stage_file) as handle:
                return handle.read().strip() or "?"
        except OSError:
            return "?"

    core = HookCore(
        roots=cfg["roots"],
        ignored=[capture],
        emit=emit,
        stage=stage,
        active=lambda: True,
        hook_dir=cfg["hook_dir"],
        python_real=os.path.realpath(sys.executable),
        track_seen=False,
    )
    sys.addaudithook(core)
    core.install_profile()
    import atexit

    atexit.register(core.check_profile)


_growin_install()


def _growin_chain():
    """If the environment already named another sitecustomize, run it after this one."""
    import importlib.machinery
    import importlib.util

    here = os.path.dirname(os.path.abspath(__file__))
    rest = [entry for entry in sys.path if os.path.abspath(entry or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


_growin_chain()
'''


def child_sitecustomize_source() -> str:
    return _CHILD_HEADER + inspect.getsource(HookCore) + _CHILD_FOOTER


# ============================================================= the file recorder


@dataclass
class Record:
    kind: str
    name: str
    stage: str
    data: bytes
    origin: str = "parent"


class _Slot:
    recorder: "FileRecorder | None" = None
    installed = False


def _dispatch(event, args):
    """The one process-wide audit hook. Inert unless a recorder is active."""
    recorder = _Slot.recorder
    if recorder is None:
        return
    recorder.core(event, args)


class FileRecorder:
    """Layer 1 for this process and every Python child started inside the `with` block."""

    def __init__(self, roots: Sequence[Path], capture: Path, hook_dir: Path) -> None:
        self.roots = [Path(os.path.realpath(root)) for root in roots]
        self.capture = Path(os.path.realpath(capture))
        self.hook_dir = hook_dir
        self.stage = "setup"
        self.records: list[Record] = []
        self._on = False
        self._saved_env: dict[str, str | None] = {}
        self._saved_profile: tuple = (None, None)
        self._thread_orig: tuple = ()
        self.core = HookCore(
            roots=self.roots,
            ignored=[self.capture],
            emit=self._emit,
            stage=lambda: self.stage,
            active=lambda: self._on,
            hook_dir=hook_dir,
            python_real=os.path.realpath(sys.executable),
        )
        for root in self.roots:
            assert not _under(self.capture, root), "the capture directory sits inside a watched root"

    def _emit(self, kind: str, name: str, stage: str, data: bytes) -> None:
        self.records.append(Record(kind, name, stage, data))

    def set_stage(self, name: str) -> None:
        self.stage = name
        (self.capture / "stage").write_text(name, encoding="utf-8")

    def __enter__(self) -> "FileRecorder":
        assert _Slot.recorder is None, "a recorder is already active"
        self.hook_dir.mkdir(parents=True, exist_ok=True)
        (self.hook_dir / "sitecustomize.py").write_text(child_sitecustomize_source(), encoding="utf-8")
        self.set_stage(self.stage)
        self._saved_env = {key: os.environ.get(key) for key in ("PYTHONPATH", "GROWIN_CANARY_HOOK")}
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = os.pathsep.join([str(self.hook_dir)] + ([old] if old else []))
        os.environ["GROWIN_CANARY_HOOK"] = json.dumps(
            {
                "roots": [str(root) for root in self.roots],
                "capture": str(self.capture),
                "hook_dir": os.path.realpath(self.hook_dir),
            }
        )
        if not _Slot.installed:
            sys.addaudithook(_dispatch)  # a process-wide hook cannot be removed; it stays inert
            _Slot.installed = True
        self._saved_profile = (sys.getprofile(), threading.getprofile())
        self.core.install_profile()
        self._patch_threads()
        self._on = True
        _Slot.recorder = self
        return self

    def _patch_threads(self) -> None:
        """A thread must start and run under the hook; one that does not is `profile-replaced`."""
        core = self.core
        orig_start, orig_run = threading.Thread.start, threading.Thread.run
        self._thread_orig = (orig_start, orig_run)

        def start(thread):
            core.check_hook("thread-start", threading.getprofile())
            return orig_start(thread)

        def run(thread):
            core.check_hook("thread-run", sys.getprofile())
            try:
                return orig_run(thread)
            finally:
                core.check_hook("thread-run", sys.getprofile())

        threading.Thread.start, threading.Thread.run = start, run

    def check_profile(self) -> None:
        self.core.check_profile()

    def __exit__(self, *exc: object) -> None:
        self.core.check_profile()  # the hook must have survived the whole run
        threading.Thread.start, threading.Thread.run = self._thread_orig
        _Slot.recorder = None
        self._on = False
        sys.setprofile(self._saved_profile[0])
        threading.setprofile(self._saved_profile[1])
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def finish(self) -> None:
        """Merge the children's records, then delete the capture directory."""
        for path in sorted(self.capture.glob("child-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                item = json.loads(line)
                self.records.append(
                    Record(item["kind"], item["name"], item["stage"], base64.b64decode(item["data"]), "child")
                )
        shutil.rmtree(self.capture, ignore_errors=True)

    # ------------------------------------------------------------------ views

    def lost_surfaces(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for record in self.records:
            if record.kind == "file-lost":
                key = f"file-lost:{record.name}@{record.stage}" + (" (child)" if record.origin == "child" else "")
                out[key] = out.get(key, b"") + record.data + b"\n"
        return out

    def problems(self) -> list[str]:
        return [f"{r.kind}:{r.name}@{r.stage}" for r in self.records if r.kind in PROBLEM_KINDS]

    def native_children(self) -> list[str]:
        return [r.name for r in self.records if r.kind == "child-native"]

    def seen_events(self) -> set[str]:
        return {r.name.split(" ", 1)[0] for r in self.records if r.kind == "seen"}


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# ============================================================ canary values, detector


@dataclass
class Canary:
    """Fresh random values per run. The relay token is read from the arm response."""

    app_key: str
    secret_key: str
    api_session: str
    userid: str
    breeze_token: str
    other_userid: str
    other_breeze_token: str
    wrong_token: str
    relay_tokens: list[str]

    @classmethod
    def new(cls) -> "Canary":
        def session_token(userid: str) -> str:
            return base64.b64encode(f"{userid}:{secrets.randbelow(10**8):08d}".encode()).decode()

        userid = f"CANARYUSER{secrets.randbelow(10**4):04d}"
        other = f"CANARYOTHER{secrets.randbelow(10**4):04d}"
        return cls(
            app_key=f"CANARYAPP{secrets.token_hex(4)}^={secrets.token_hex(2)}#",
            secret_key=f"CANARYSECRET{secrets.token_hex(6)}",
            api_session=f"CANARYCODE{secrets.randbelow(10**8):08d}",
            userid=userid,
            breeze_token=session_token(userid),
            other_userid=other,
            other_breeze_token=session_token(other),
            wrong_token=secrets.token_urlsafe(32),
            relay_tokens=[],
        )

    def values(self) -> dict[str, list[str]]:
        return {
            "app_key": [self.app_key],
            "secret_key": [self.secret_key],
            "api_session": [self.api_session],
            "userid": [self.userid],
            "breeze_token": [self.breeze_token],
            "other_userid": [self.other_userid],
            "other_breeze_token": [self.other_breeze_token],
            "wrong_token": [self.wrong_token],
            "relay_token": list(self.relay_tokens),
        }


def expand_forms(value: str) -> list[bytes]:
    """The value as it could appear: raw, URL-quoted, base64, JSON-escaped."""
    raw = value.encode("utf-8")
    std = base64.b64encode(raw).decode()
    url = base64.urlsafe_b64encode(raw).decode()
    forms = {
        value,
        quote_plus(value),
        quote(value),
        quote(value, safe=""),
        std,
        url,
        json.dumps(value)[1:-1],
    }
    return sorted(form.encode("utf-8") for form in forms if form)


def find_leaks(
    surfaces: Mapping[str, bytes],
    values: Mapping[str, Sequence[str]],
    allowed: Callable[[str, str], bool] = lambda surface, name: False,
) -> list[tuple[str, str]]:
    """Every (surface, value name) where a value shows up in any form, minus the allowed places."""
    found: set[tuple[str, str]] = set()
    forms = {name: [form for value in group for form in expand_forms(value)] for name, group in values.items()}
    for surface, data in surfaces.items():
        for name, group in forms.items():
            if any(form in data for form in group) and not allowed(surface, name):
                found.add((surface, name))
    return sorted(found)


def canary_allowed(surface: str, name: str) -> bool:
    """The designed places, per path: the relay token in its file and its temp file, the AppKey in the opener's URL."""
    if name == "relay_token":
        match = SURFACE_FILE_RE.match(surface)
        return bool(match) and (match["rel"] == TOKEN_REL or bool(TOKEN_TEMP_RE.match(match["rel"])))
    if name == "app_key":
        return surface == "opener-arg"
    return False


RELAY_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)


def scrub_arm_response(data: dict, canary: "Canary") -> dict:
    """Strip only the designed content from an arm response, and leave the rest to be scanned.

    login_url may hold the AppKey, URL-quoted, and nothing else secret: only that exact form is
    removed. relay_token is the token and nothing more: a field that is not exactly a token-shaped
    value is left whole, so it is reported (its content is a leak by definition).
    """
    out = dict(data)
    out["login_url"] = out["login_url"].replace(quote_plus(canary.app_key), "<designed: app key>")
    if RELAY_TOKEN_SHAPE.fullmatch(out["relay_token"]):
        out["relay_token"] = "<designed: relay token>"
    return out


@dataclass
class Report:
    surfaces: dict[str, bytes]
    values: dict[str, list[str]]
    problems: list[str]
    native_children: list[str]

    @property
    def leaks(self) -> list[tuple[str, str]]:
        return find_leaks(self.surfaces, self.values, canary_allowed)

    def leaks_where(self, keep: Callable[[str], bool]) -> list[tuple[str, str]]:
        subset = {name: data for name, data in self.surfaces.items() if keep(name)}
        return find_leaks(subset, self.values, canary_allowed)

    def temp_files_at_boundaries(self) -> list[str]:
        out = []
        for name in self.surfaces:
            match = SURFACE_FILE_RE.match(name)
            if name.startswith("file:") and match and TOKEN_TEMP_RE.match(match["rel"]):
                out.append(name)
        return out

    def assert_clean(self) -> None:
        assert not self.problems, f"run problems: {self.problems}"
        assert not self.leaks, f"leaks: {self.leaks}"
        assert not self.temp_files_at_boundaries(), "a token-store temp file outlived its rename"


# ========================================================= the in-process VM world


def breeze_body(success, status=200, error=None) -> bytes:
    return json.dumps({"Success": success, "Status": status, "Error": error}).encode()


def customer_body(userid: str, token: str) -> bytes:
    return breeze_body(
        {
            "idirect_userid": userid,
            "session_token": token,
            "segments_allowed": {"Trading": "Y", "Equity": "Y"},
            "exg_trade_date": {"NSE": "21-Oct-2026"},
        }
    )


def _bars_rows(minute: bool) -> list[dict]:
    if minute:
        return [
            {
                "datetime": f"2026-10-21 09:{15 + n:02d}:00",
                "stock_code": "RELIND",
                "exchange_code": "NSE",
                "open": 1400.5,
                "high": 1401.0,
                "low": 1400.0,
                "close": 1400.75,
                "volume": 100 + n,
            }
            for n in range(5)
        ]
    return [
        {
            "datetime": "2025-06-02 00:00:00",
            "stock_code": "RELIND",
            "exchange_code": "NSE",
            "open": 1.5,
            "high": 2.5,
            "low": 1.25,
            "close": 2.0,
            "volume": 10,
        }
    ]


QUOTE_KEYS = (
    "best_bid_price", "best_bid_quantity", "best_offer_price", "best_offer_quantity", "exchange_code",
    "expiry_date", "high", "low", "lower_circuit", "ltp", "ltp_percent_change", "ltt", "open",
    "previous_close", "product_type", "right", "spot_price", "stock_code", "strike_price",
    "total_quantity_traded", "upper_circuit",
)
PREVIEW_KEYS = (
    "brokerage", "exchange_turnover_charges", "stamp_duty", "stt", "sebi_charges", "gst",
    "total_turnover_and_sebi_charges", "total_other_charges", "total_brokerage",
)


def _quote_row() -> dict:
    fixed = {
        "stock_code": "RELIND",
        "exchange_code": "NSE",
        "product_type": "cash",
        "ltt": "21-Oct-2026 15:55:21",
        "expiry_date": "",
        "right": "",
        "strike_price": "",
    }
    return {key: fixed.get(key, 1.5 + index) for index, key in enumerate(QUOTE_KEYS)}


class FakeClock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = T0

    def monotonic(self) -> float:
        return self.mono

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


class CanaryTransport:
    """The Breeze side of the world: a fake network that answers the reads and records every request."""

    def __init__(self, canary: Canary) -> None:
        self.canary = canary
        self.requests: list[tuple[str, str]] = []
        self.customer = HttpResponse(200, {}, customer_body(canary.userid, canary.breeze_token))
        self.bars_daily = HttpResponse(200, {}, breeze_body(_bars_rows(False)))
        self.bars_minute = HttpResponse(200, {}, breeze_body(_bars_rows(True)))
        self.quotes = HttpResponse(200, {}, breeze_body([_quote_row()]))
        self.preview = HttpResponse(200, {}, breeze_body({key: 0.5 + i for i, key in enumerate(PREVIEW_KEYS)}))
        self.master = HttpResponse(
            200, {"last-modified": "Tue, 20 Oct 2026 09:30:00 GMT"}, b"PK\x03\x04" + bytes(range(64))
        )
        self.lock = threading.Lock()

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        with self.lock:
            self.requests.append((method, url))
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path
        if host in ("api.ipify.org", "metadata.google.internal"):
            return HttpResponse(200, {}, EXPECTED_IP.encode())
        if host == "api.icicidirect.com" and path.endswith("/customerdetails"):
            return self.customer
        if host == "api.icicidirect.com" and path.endswith("/quotes"):
            return self.quotes
        if host == "api.icicidirect.com" and path.endswith("/preview_order"):
            return self.preview
        if host == "directlink.icicidirect.com":
            return self.master
        if host == "breezeapi.icicidirect.com":
            return self.bars_minute if "interval=1minute" in parts.query else self.bars_daily
        raise AssertionError(f"the canary world reached an unexpected host: {host}")


class FakeSecrets:
    def __init__(self, canary: Canary) -> None:
        self._values = {"canary-app-key": canary.app_key, "canary-secret-key": canary.secret_key}

    def read(self, name: str) -> str:
        return self._values[name]

    def discard(self) -> None:
        pass


class FlakyCounterStore(FileCounterStore):
    """The real counter file, with a switch that makes it unreadable (BUDGET_UNAVAILABLE)."""

    fail = False

    def load(self):
        if self.fail:
            raise CounterUnreadable
        return super().load()

    def save(self, ist_date, count):
        if self.fail:
            raise CounterUnreadable
        super().save(ist_date, count)


class KillFake(ThreadingHTTPServer):
    """A loopback stand-in for the metadata kill switch. GET only, answers `enabled`."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _KillHandler)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _KillHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        data = b"enabled" if self.path == KILL_PATH else b"not found"
        self.send_response(200 if self.path == KILL_PATH else 404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SharedHttp:
    """The TestClient the Mac client talks to. A RelayClient closing its pool must not close it."""

    def __init__(self, inner: httpx.Client) -> None:
        self._inner = inner

    def stream(self, method, path, **kwargs):
        return self._inner.stream(method, path, **kwargs)

    def close(self) -> None:
        pass


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            parts = [record.name, record.levelname, record.getMessage(), repr(record.args)]
        except Exception:
            parts = [record.name, record.levelname, str(record.msg)]
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        if record.stack_info:
            parts.append(record.stack_info)
        self.lines.append(" | ".join(parts))


class World:
    """One canary run: the VM app, the Mac client, all capture, and the file recorder."""

    def __init__(
        self,
        tmp_path: Path,
        capture: Path,
        capfd,
        monkeypatch,
        *,
        order_config: bool = True,
    ) -> None:
        self.tmp = tmp_path
        self.capfd = capfd
        self.canary = Canary.new()
        self.clock = FakeClock()
        self.current = "setup"
        self.plants: dict[str, list[Callable[["World"], None]]] = {}
        self.opener_plant: Callable[["World", str], None] | None = None
        self.opened: list[str] = []
        self._surfaces: dict[str, bytearray] = {}
        self._lock = threading.Lock()
        self._clients: list[RelayClient] = []

        self.home = tmp_path / "home"
        self.tmpdir = tmp_path / "tmp"
        self.home.mkdir()
        self.tmpdir.mkdir()
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("TMPDIR", str(self.tmpdir))
        monkeypatch.setattr(tempfile, "tempdir", None)  # re-read TMPDIR; monkeypatch restores it

        vm = tmp_path / "vm"
        self.state_dir = vm / "state"
        self.counter_dir = vm / "counter"
        for directory in (vm, self.state_dir, self.counter_dir):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o700)
        limits_path, key_path = vm / "limits.json", vm / "order-key.json"
        if order_config:
            for path, payload in (
                (limits_path, VECTORS["limits"]),
                (key_path, {"key_id": PRIMARY["key_id"], "public_key_x963_hex": PRIMARY["public_key_x963_hex"]}),
            ):
                path.write_text(json.dumps(payload), encoding="ascii")
                os.chmod(path, 0o600)

        self.kill = KillFake()
        self._kill_thread = threading.Thread(
            target=self.kill.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._kill_thread.start()

        self.transport = CanaryTransport(self.canary)
        self.sink = io.StringIO()
        self._sink_seen = 0
        self.store = FlakyCounterStore(self.counter_dir)
        config = RelayConfig(
            expected_egress_ip=EXPECTED_IP,
            expected_userid=self.canary.userid,
            secret_names={"app_key": "canary-app-key", "secret_key": "canary-secret-key"},
            limits_path=str(limits_path),
            order_key_path=str(key_path),
        )
        self.app = build_app(
            config,
            transport=self.transport,
            secret_source=FakeSecrets(self.canary),
            clock=self.clock,
            events=EventLog(self.sink, self.clock),
            store=self.store,
            state_dir=self.state_dir,
            order_overrides=OrderSettings(
                state_dir=self.state_dir,
                limits_path=str(limits_path),
                key_path=str(key_path),
                owner_uid=os.getuid(),
                allow_test_key=True,
                kill_base_url=self.kill.base,
                holidays=frozenset(),
                special_sessions=frozenset(),
            ),
        )
        self.http = TestClient(self.app, base_url=BASE)
        self.http.event_hooks["response"] = [self._on_response]
        self.token_path = self.home / ".config" / "growin" / "relay-session.json"
        assert self.token_path.relative_to(tmp_path).as_posix() == TOKEN_REL

        self.recorder = FileRecorder([tmp_path], capture, tmp_path / "child-hook")
        self._log = _ListHandler()

    # ------------------------------------------------------------------ capture

    def add(self, surface: str, data: bytes | str) -> None:
        raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
        with self._lock:
            self._surfaces.setdefault(surface, bytearray()).extend(raw + b"\n")

    def _on_response(self, response: httpx.Response) -> None:
        response.read()
        body = response.content
        if response.request.url.path == "/v1/login/arm" and response.status_code == 200:
            data = json.loads(body)
            self.canary.relay_tokens.append(data["relay_token"])
            assert data["relay_token"] not in data["login_url"]
            assert quote_plus(self.canary.app_key) in data["login_url"]
            # Only the AppKey inside login_url and the token itself are stripped (see scrub_arm_response).
            body = json.dumps(scrub_arm_response(data, self.canary)).encode()
        head = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
        self.add(
            f"resp@{self.current}",
            f"{response.request.method} {response.request.url} {response.status_code}\n{head}\n".encode() + body,
        )

    def record_exc(self, err: BaseException) -> None:
        detail = f"{type(err).__name__} {err!s} {err!r} {getattr(err, '__dict__', {})!r}"
        self.add(f"exc@{self.current}", detail)

    def _drain(self, stage: str) -> None:
        out, err = self.capfd.readouterr()
        self.add(f"stdout@{stage}", out)
        self.add(f"stderr@{stage}", err)
        lines, self._log.lines = self._log.lines, []
        self.add(f"log@{stage}", "\n".join(lines))
        text = self.sink.getvalue()
        self.add(f"vm-events@{stage}", text[self._sink_seen :])
        self._sink_seen = len(text)

    def _snapshot_files(self, stage: str) -> None:
        for dirpath, _dirs, files in os.walk(self.tmp):
            for name in files:
                full = Path(dirpath) / name
                try:
                    if not stat.S_ISREG(os.lstat(full).st_mode):
                        continue
                    data = full.read_bytes()
                except OSError:
                    continue
                self.add(f"file:{full.relative_to(self.tmp).as_posix()}@{stage}", data)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        self.current = name
        self.recorder.set_stage(name)
        try:
            yield
        finally:
            self.recorder.check_profile()
            self._drain(name)
            self._snapshot_files(name)

    @contextmanager
    def session(self) -> Iterator["World"]:
        """Capture logging and warnings, and run the recorder, for the length of the block."""
        root = logging.getLogger()
        old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self._log)
        logging.captureWarnings(True)
        try:
            with self.recorder:
                yield self
        finally:
            logging.captureWarnings(False)
            root.removeHandler(self._log)
            root.setLevel(old_level)

    def finish(self) -> Report:
        self.recorder.finish()
        surfaces = {name: bytes(data) for name, data in self._surfaces.items()}
        surfaces.update(self.recorder.lost_surfaces())
        return Report(surfaces, self.canary.values(), self.recorder.problems(), self.recorder.native_children())

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self.http.close()
        self.kill.shutdown()
        self.kill.server_close()

    # ------------------------------------------------------------------ the Mac side

    def token_store(self) -> FileTokenStore:
        return FileTokenStore(self.token_path, now=lambda: self.clock.now)

    def new_client(self) -> RelayClient:
        client = RelayClient(http=SharedHttp(self.http), token_store=self.token_store())  # type: ignore[arg-type]
        self._clients.append(client)
        return client

    def _factory(self) -> RelayClient:
        for plant in self.plants.pop(self.current, []):
            plant(self)  # a planted component acting inside the CLI call of this stage
        return self.new_client()

    def _opener(self, url: str) -> None:
        self.opened.append(url)
        self.add("opener-arg", url)
        if self.opener_plant is not None:
            self.opener_plant(self, url)

    def cli(self, argv: list[str], *, isatty: bool = True) -> tuple[int, str]:
        out = io.StringIO()
        code = cli_mod.main(
            argv,
            client_factory=self._factory,
            getpass_fn=lambda _prompt: self.canary.api_session,
            opener=self._opener,
            isatty=lambda: isatty,
            out=out,
        )
        self.add(f"cli-out@{self.current}", out.getvalue())
        return code, out.getvalue()

    def raw(self, method: str, path: str, *, token: str | None, orders: bool = False, body=None):
        headers = {contract.HDR_CONTRACT: ORDERS_CONTRACT if orders else contract.CONTRACT}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.http.request(method, path, headers=headers, json=body)

    def events_named(self, name: str) -> list[dict]:
        return [json.loads(line) for line in self.sink.getvalue().splitlines() if f'"{name}"' in line]


@pytest.fixture()
def make_world(tmp_path, tmp_path_factory, capfd, monkeypatch):
    built: list[World] = []

    def build(**options) -> World:
        world = World(tmp_path, tmp_path_factory.mktemp("capture"), capfd, monkeypatch, **options)
        built.append(world)
        return world

    yield build
    for world in built:
        world.close()


# ================================================================== the flows


def run_flow_a(world: World) -> None:
    """The CLI path: login --paste twice, every read command, then revoke."""
    with world.stage("login"):
        code, out = world.cli(["login", "--paste", "--no-open"])
        assert code == 0, out
        assert world.opened == []
        code, out = world.cli(["login", "--paste"])
        assert code == 0, out
        assert len(world.opened) == 1
    for stage, argv in (
        ("health", ["health"]),
        ("budget", ["budget"]),
        ("orders-state", ["orders-state"]),
        ("orders-key", ["orders-key"]),
        ("orders-audit", ["orders-audit"]),
    ):
        with world.stage(stage):
            code, out = world.cli(argv)
            assert code == 0, out
    with world.stage("pre-revoke"):
        pass  # a snapshot immediately before revoke deletes the token file
    with world.stage("revoke"):
        code, out = world.cli(["revoke"])
        assert code == 0, out


def run_flow_b(world: World) -> None:
    """The client path: every call RelayClient and OrdersClient make, one stage each."""
    relay = world.new_client()
    with world.stage("arm"):
        attempt = relay.login_arm()
    with world.stage("complete"):
        relay.login_complete(attempt.attempt_id, world.canary.api_session)
    with world.stage("daily-bars"):
        relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13))
    with world.stage("intraday-bars"):
        relay.intraday_bars("RELIND", date(2026, 10, 21))
    with world.stage("quote"):
        relay.quote("RELIND")
    with world.stage("preview-order"):
        relay.preview_order("RELIND", action="buy", quantity=35, price=Decimal("1402.30"))
    with world.stage("security-master"):
        relay.security_master()
    with world.stage("budget"):
        relay.budget()
    with world.stage("health"):
        relay.health()
    orders = OrdersClient(relay)
    with world.stage("orders-state"):
        orders.state()
    with world.stage("orders-audit"):
        orders.audit()
    with world.stage("orders-key"):
        orders.current_key()
    with world.stage("orders-halt"):
        orders.halt()
    with world.stage("pre-revoke"):
        pass
    with world.stage("revoke"):
        relay.revoke()


def expect_relay_error(world: World, call: Callable[[], object], code: str) -> RelayError:
    try:
        call()
    except RelayError as err:
        world.record_exc(err)
        assert err.code == code, f"expected {code}, got {err.code}"
        return err
    raise AssertionError(f"expected {code}, the call succeeded")


def full_login(world: World, relay: RelayClient) -> None:
    attempt = relay.login_arm()
    relay.login_complete(attempt.attempt_id, world.canary.api_session)


def run_failure_paths(world: World) -> None:
    cn = world.canary
    relay = world.new_client()
    good_customer = world.transport.customer

    with world.stage("wrong-account"):
        attempt = relay.login_arm()
        world.transport.customer = HttpResponse(200, {}, customer_body(cn.other_userid, cn.other_breeze_token))
        expect_relay_error(world, lambda: relay.login_complete(attempt.attempt_id, cn.api_session), "IDENTITY_MISMATCH")
        world.transport.customer = good_customer

    with world.stage("code-fails-filter"):
        attempt = relay.login_arm()
        # The client filters the code before sending; go around it so the VM's own 422 is exercised.
        resp = world.raw(
            "POST",
            "/v1/login/complete",
            token=cn.relay_tokens[-1],
            body={"attempt_id": attempt.attempt_id, "apisession": cn.api_session + "!"},
        )
        assert resp.status_code == 422, resp.text

    with world.stage("login-ok"):
        full_login(world, relay)

    with world.stage("wrong-bearer-data"):
        resp = world.raw("GET", "/v1/bars/daily?stock_code=RELIND&from=2025-06-02&to=2025-06-13", token=cn.wrong_token)
        assert resp.status_code == 401, resp.text
    with world.stage("wrong-bearer-orders"):
        resp = world.raw("GET", "/v1/orders/state", token=cn.wrong_token, orders=True)
        assert resp.status_code == 401, resp.text

    with world.stage("breeze-error-body"):
        good_bars = world.transport.bars_daily
        world.transport.bars_daily = HttpResponse(
            200, {}, breeze_body(None, status=500, error=f"bad session {cn.breeze_token} for {cn.app_key}")
        )
        err = expect_relay_error(world, lambda: relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13)), "UPSTREAM_ERROR")
        assert err.http_status == 502
        world.transport.bars_daily = good_bars

    with world.stage("bars-after-revoke"):
        stale = cn.relay_tokens[-1]
        relay.revoke()
        resp = world.raw("GET", "/v1/bars/daily?stock_code=RELIND&from=2025-06-02&to=2025-06-13", token=stale)
        assert resp.status_code in (401, 503), resp.text

    with world.stage("counter-unreadable"):
        full_login(world, relay)
        world.store.fail = True
        try:
            err = expect_relay_error(
                world, lambda: relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13)), "BUDGET_UNAVAILABLE"
            )
            assert err.http_status == 503
        finally:
            world.store.fail = False

    with world.stage("no-tty-login"):
        armed_before = len(world.events_named("login_armed"))
        code, out = world.cli(["login", "--paste"], isatty=False)
        assert code == cli_mod.EXIT_USAGE and "interactive terminal" in out
        assert len(world.events_named("login_armed")) == armed_before  # nothing was armed

    with world.stage("pre-revoke"):
        pass
    with world.stage("revoke"):
        relay.revoke()


def run_config_invalid(world: World) -> None:
    relay = world.new_client()
    with world.stage("login"):
        full_login(world, relay)
    orders = OrdersClient(relay)
    for stage, call in (
        ("orders-state", orders.state),
        ("orders-audit", orders.audit),
        ("orders-halt", orders.halt),
    ):
        with world.stage(stage):
            err = expect_relay_error_any(world, call)
            assert err.http_status == 503
    with world.stage("pre-revoke"):
        pass
    with world.stage("revoke"):
        relay.revoke()


def expect_relay_error_any(world: World, call: Callable[[], object]) -> RelayError:
    try:
        call()
    except RelayError as err:
        world.record_exc(err)
        return err
    raise AssertionError("the call succeeded")


def executed(world: World, flow: Callable[[World], None]) -> Report:
    with world.session():
        flow(world)
    return world.finish()


# ================================================================ the real checks


def test_c1_the_cli_paste_login_flow_leaks_nothing(make_world, record_property):
    world = make_world()
    report = executed(world, run_flow_a)
    report.assert_clean()
    assert report.native_children == []  # a new native child is an RS-1 blind spot worth a review comment
    record_property("native_children", ",".join(report.native_children) or "none")
    # The designed place did receive the token, so the allowance above was exercised, not vacuous.
    assert any(name.startswith(f"file:{TOKEN_REL}@") for name in report.surfaces)
    assert world.canary.relay_tokens, "the run never armed"


def test_c2_the_client_flow_leaks_nothing(make_world, record_property):
    world = make_world()
    report = executed(world, run_flow_b)
    report.assert_clean()
    assert report.native_children == []
    record_property("native_children", ",".join(report.native_children) or "none")
    assert any(
        entry.get("event") == "request" and entry.get("route") == "/v1/bars/intraday"
        for entry in world.events_named("request")
    )


def test_c3_every_failure_path_leaks_nothing(make_world):
    world = make_world()
    report = executed(world, run_failure_paths)
    report.assert_clean()
    bodies = bytes(world._surfaces["resp@wrong-bearer-data"]) + bytes(world._surfaces["resp@wrong-bearer-orders"])
    assert world.canary.wrong_token.encode() not in bodies
    assert all(token.encode() not in bodies for token in world.canary.relay_tokens)


def test_c3_order_routes_with_config_invalid_leak_nothing(make_world):
    world = make_world(order_config=False)
    report = executed(world, run_config_invalid)
    report.assert_clean()
    assert any(e.get("reason") == "config_invalid" for e in world.events_named("config_alarm"))


def test_c5_revoke_clears_the_token_file_and_a_refused_login_arms_nothing(make_world):
    world = make_world()
    report = executed(world, run_flow_a)
    report.assert_clean()
    assert not world.token_path.exists() or b"token" not in world.token_path.read_bytes()
    assert world.events_named("login_armed")  # the real logins did arm
    # The file existed during the run (it was scanned) and was gone after revoke.
    assert any(name.startswith(f"file:{TOKEN_REL}@orders-audit") for name in report.surfaces)
    assert not any(name.startswith(f"file:{TOKEN_REL}@revoke") for name in report.surfaces)


# --------------------------------------------------------- the detector itself


def test_c4_the_detector_reports_exactly_the_planted_surface_and_value(make_world):
    world = make_world()

    def plant(w: World) -> None:
        with w.stage("plant"):
            logging.getLogger("canary.negative").warning("session token is %s", w.canary.breeze_token)

    report = executed(world, plant)
    assert report.leaks == [("log@plant", "breeze_token")]


@pytest.mark.parametrize("form", ["raw", "quote_plus", "quote", "base64", "json"])
def test_c4_every_encoding_of_a_value_is_found(form):
    value = "CANARYAPPab12cd34^=ef56#"
    shaped = {
        "raw": value,
        "quote_plus": quote_plus(value),
        "quote": quote(value, safe=""),
        "base64": base64.b64encode(value.encode()).decode(),
        "json": json.dumps(value)[1:-1],
    }[form]
    surfaces = {"somewhere": f"prefix {shaped} suffix".encode(), "elsewhere": b"nothing here"}
    assert find_leaks(surfaces, {"app_key": [value]}) == [("somewhere", "app_key")]


def test_c4_the_allowed_places_are_per_path_not_per_directory():
    token = "T" * 43
    surfaces = {
        f"file:{TOKEN_REL}@login": token.encode(),
        "file:home/.config/growin/.relay-session.json.4242.deadbeef.tmp@login": token.encode(),
        "file:home/.config/growin/planted.txt@login": token.encode(),
        "opener-arg": b"https://x/?k=abc",
    }
    got = find_leaks(surfaces, {"relay_token": [token], "app_key": ["abc"]}, canary_allowed)
    assert got == [("file:home/.config/growin/planted.txt@login", "relay_token")]


# ------------------------------------------------- write-then-delete controls


def plant_file(world: World, name: str, text: str) -> Path:
    path = world.home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_control_a_a_url_written_and_unlinked_by_the_opener_is_found_at_the_delete(make_world):
    world = make_world()

    def opener_plant(w: World, url: str) -> None:
        planted = Path(os.path.expanduser("~/.cache/growin-x"))  # HOME points into the watched root
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(url, encoding="utf-8")
        planted.unlink()

    world.opener_plant = opener_plant
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/.cache/growin-x@login", "app_key") in report.leaks
    # The old design, a scan after the run only, finds nothing: this is the proof it missed it.
    assert report.leaks_where(lambda name: name.startswith("file:") and name.endswith("@revoke")) == []


def test_control_b_a_file_replaced_by_an_empty_one_is_read_at_the_rename(make_world):
    world = make_world()

    def plant(w: World) -> None:
        planted = plant_file(w, ".config/growin/planted.txt", w.canary.relay_tokens[-1])
        handle, temp = tempfile.mkstemp()  # TMPDIR points into the watched root
        os.close(handle)
        assert Path(temp).parent == w.tmpdir
        os.replace(temp, planted)

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    assert ("file-lost:home/.config/growin/planted.txt@health", "relay_token") in report.leaks
    # Only that planted path is reported for the token: the token file itself stays allowed.
    assert {s for s, n in report.leaks if n == "relay_token"} == {"file-lost:home/.config/growin/planted.txt@health"}


def test_control_c_a_file_opened_for_truncating_write_is_read_at_the_open(make_world):
    world = make_world()

    def plant(w: World) -> None:
        planted = plant_file(w, "scratch/session.txt", w.canary.api_session)
        with open(planted, "w", encoding="utf-8"):
            pass

    world.plants["budget"] = [plant]
    report = executed(world, run_flow_a)
    assert ("file-lost:home/scratch/session.txt@budget", "api_session") in report.leaks


def test_control_d_a_native_child_write_survives_only_to_the_stage_snapshots(make_world):
    world = make_world()
    target = world.home / "native-child.txt"

    def write_it(w: World) -> None:
        subprocess.run(["/bin/sh", "-c", 'printf %s "$1" > "$2"', "sh", w.canary.userid, str(target)], check=True)

    def delete_it(w: World) -> None:
        subprocess.run(["/bin/sh", "-c", 'rm -f "$1"', "sh", str(target)], check=True)

    world.plants["orders-key"] = [write_it]
    world.plants["revoke"] = [delete_it]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert report.native_children.count("sh") == 2  # listed for review: RS-1, not a failure
    assert ("file:home/native-child.txt@pre-revoke", "userid") in report.leaks
    assert not any(name.startswith("file-lost:home/native-child.txt") for name in report.surfaces)  # the hook never saw it
    assert not target.exists()  # and it was gone by the end, so an after-run scan would find nothing


def python_child(code: str, *args: str, flags: tuple[str, ...] = ()) -> None:
    subprocess.run([sys.executable, *flags, "-c", code, *args], check=True)


CHILD_WRITES_AND_DELETES = (
    "import pathlib, sys\n"
    "p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)\n"
    "p.write_text(sys.argv[2]); p.unlink()\n"
)


def test_control_f_a_python_child_that_writes_and_deletes_in_one_stage_is_found(make_world):
    world = make_world()
    target = world.home / "py-child.txt"

    def plant(w: World) -> None:
        python_child(CHILD_WRITES_AND_DELETES, str(target), w.canary.relay_tokens[-1])

    world.plants["orders-key"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/py-child.txt@orders-key (child)", "relay_token") in report.leaks
    # Layer 2 alone misses it, which is the gap the plan's r2 had.
    assert report.leaks_where(lambda name: name.startswith("file:")) == []
    assert not target.exists()


def test_control_g_a_python_child_that_skips_sitecustomize_fails_the_run(make_world):
    world = make_world()

    def plant(w: World) -> None:
        python_child("pass", flags=("-I",))  # wrote nothing, and still a failure

    world.plants["orders-key"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems and all(p.startswith("child-unwatched:") and p.endswith("@orders-key") for p in report.problems)
    with pytest.raises(AssertionError, match="child-unwatched"):
        report.assert_clean()


@pytest.mark.parametrize("flag", ["-I", "-E", "-S"])
def test_every_flag_that_skips_sitecustomize_is_unwatched(tmp_path, tmp_path_factory, flag):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    with recorder:
        subprocess.run([sys.executable, flag, "-c", "pass"], check=True)
    recorder.finish()
    assert [p.split(":")[0] for p in recorder.problems()] == ["child-unwatched"]


def test_a_python_child_without_the_hook_directory_in_its_environment_is_unwatched(tmp_path, tmp_path_factory):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    with recorder:
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "GROWIN_CANARY_HOOK")}
        subprocess.run([sys.executable, "-c", "pass"], env=env, check=True)
    recorder.finish()
    assert [p.split(":")[0] for p in recorder.problems()] == ["child-unwatched"]


def test_a_watched_python_child_is_not_a_problem(tmp_path, tmp_path_factory):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    with recorder:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    recorder.finish()
    assert recorder.problems() == []



# ------------------------------------------ r1 review rows: truncate, size, fork, arm fields

# (mode, open kwargs, flush before truncate). `file.truncate()` fires no audit event on 3.11.
TRUNCATE_SHAPES = {
    "text-flushed": ("w", {}, True),
    "text-unflushed": ("w", {}, False),
    "buffered-binary": ("wb", {}, True),
    "raw-unbuffered": ("wb", {"buffering": 0}, False),
}


@pytest.mark.parametrize("shape", sorted(TRUNCATE_SHAPES))
def test_control_h_a_value_written_truncated_and_unlinked_in_one_stage_is_found(make_world, shape):
    mode, kwargs, flush = TRUNCATE_SHAPES[shape]
    world = make_world()
    target = world.home / "scratch" / "trunc.txt"

    def plant(w: World) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        token = w.canary.relay_tokens[-1]
        with open(target, mode, **kwargs) as handle:
            handle.write(token if "b" not in mode else token.encode())
            if flush:
                handle.flush()
            handle.truncate(0)  # no audit event; the file is empty from here
        target.unlink()

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/scratch/trunc.txt@health", "relay_token") in report.leaks
    assert "file.truncate" in world.recorder.seen_events()
    # The stage snapshots cannot see it: the file is empty at the boundary, then gone.
    assert report.leaks_where(lambda name: name.startswith("file:")) == []
    assert not target.exists()


CHILD_TRUNCATES_AND_DELETES = (
    "import pathlib, sys\n"
    "p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True)\n"
    "with open(p, 'w') as handle:\n"
    "    handle.write(sys.argv[2]); handle.truncate(0)\n"
    "p.unlink()\n"
)


def test_control_h_a_python_child_that_truncates_and_deletes_is_found(make_world):
    world = make_world()
    target = world.home / "py-trunc.txt"

    def plant(w: World) -> None:
        python_child(CHILD_TRUNCATES_AND_DELETES, str(target), w.canary.relay_tokens[-1])

    world.plants["orders-key"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/py-trunc.txt@orders-key (child)", "relay_token") in report.leaks


def test_a_file_object_with_no_descriptor_is_not_an_error(tmp_path, tmp_path_factory):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    with recorder:
        recorder.set_stage("memory")
        buffer = io.BytesIO(b"canary-in-memory")
        buffer.truncate(0)
        text = io.StringIO("canary-in-memory")
        text.truncate(0)
    recorder.finish()
    assert recorder.problems() == [] and recorder.lost_surfaces() == {}


@pytest.mark.parametrize("padding", [4 * HookCore.CHUNK - 5, 5 * HookCore.CHUNK + 3])
def test_control_j_a_value_past_the_old_4_mib_cut_is_still_found(make_world, padding):
    world = make_world()
    target = world.home / "scratch" / "big.bin"

    def plant(w: World) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # With the smaller padding the value straddles both the old 4 MiB limit and a chunk boundary.
        target.write_bytes(b"A" * padding + w.canary.api_session.encode() + b"B" * 64)
        target.unlink()

    world.plants["budget"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/scratch/big.bin@budget", "api_session") in report.leaks


def test_a_file_past_the_read_limit_fails_the_run_instead_of_being_cut_off(make_world, monkeypatch):
    monkeypatch.setattr(HookCore, "MAX_READ", 1024 * 1024)
    world = make_world()
    target = world.home / "scratch" / "huge.bin"

    def plant(w: World) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"A" * (2 * 1024 * 1024) + w.canary.api_session.encode())
        target.unlink()

    world.plants["budget"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == ["file-too-large:home/scratch/huge.bin@budget"]
    with pytest.raises(AssertionError, match="file-too-large"):
        report.assert_clean()


@pytest.mark.parametrize("call", ["fork", "forkpty"])
def test_control_k_a_forked_child_fails_the_run(make_world, call):
    world = make_world()
    target = world.home / "forked.txt"

    def plant(w: World) -> None:
        try:
            result = os.fork() if call == "fork" else os.forkpty()
        except OSError:
            return  # the audit event fires before the call, which is what is under test
        pid = result if call == "fork" else result[0]
        if pid == 0:  # the child writes and deletes; its records die with it
            try:
                target.write_text(w.canary.relay_tokens[-1], encoding="utf-8")
                target.unlink()
            finally:
                os._exit(0)
        os.waitpid(pid, 0)
        if call == "forkpty":
            os.close(result[1])

    world.plants["orders-key"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == [f"child-unwatched:os.{call}@orders-key"]
    assert not any("forked.txt" in name for name in report.surfaces)  # the child's loss never arrived
    with pytest.raises(AssertionError, match="child-unwatched"):
        report.assert_clean()


# ------------------------------------------ r2 review rows: the hook must still be installed


def write_truncate_delete(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        handle.truncate(0)
    path.unlink()


def test_control_m_code_that_removes_the_profile_hook_fails_the_run(make_world):
    world = make_world()
    target = world.home / "scratch" / "blind.txt"

    def plant(w: World) -> None:
        sys.setprofile(None)
        write_truncate_delete(target, w.canary.relay_tokens[-1])

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    assert "profile-replaced:sys@health" in report.problems
    assert all(p.startswith("profile-replaced:sys@") for p in report.problems), report.problems
    assert report.problems[-1].endswith("@revoke")  # still reported at the exit check
    assert not [leak for leak in report.leaks if "blind.txt" in leak[0]]  # the truncate went unseen  # the truncate really went unseen
    with pytest.raises(AssertionError, match="profile-replaced"):
        report.assert_clean()


def test_control_m_a_thread_started_after_threading_setprofile_none_fails_the_run(make_world):
    world = make_world()
    target = world.home / "scratch" / "blind-thread.txt"

    def plant(w: World) -> None:
        threading.setprofile(None)
        thread = threading.Thread(target=write_truncate_delete, args=(target, w.canary.relay_tokens[-1]))
        thread.start()
        thread.join()

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    for expected in ("thread-start", "thread-run", "threading"):
        assert f"profile-replaced:{expected}@health" in report.problems, (expected, report.problems)
    assert not [leak for leak in report.leaks if "blind-thread.txt" in leak[0]]  # the truncate went unseen
    with pytest.raises(AssertionError, match="profile-replaced"):
        report.assert_clean()


def test_control_m_a_thread_that_removes_the_hook_itself_fails_the_run(make_world):
    world = make_world()
    target = world.home / "scratch" / "blind-inner.txt"

    def plant(w: World) -> None:
        def body() -> None:
            sys.setprofile(None)  # only this thread loses the hook
            write_truncate_delete(target, w.canary.relay_tokens[-1])

        thread = threading.Thread(target=body)
        thread.start()
        thread.join()

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == ["profile-replaced:thread-run@health"]
    assert not [leak for leak in report.leaks if "blind-inner.txt" in leak[0]]  # the truncate went unseen


def test_control_m_a_python_child_that_removes_the_hook_fails_the_run(make_world):
    world = make_world()
    target = world.home / "py-blind.txt"
    code = "import sys\nsys.setprofile(None)\n" + CHILD_TRUNCATES_AND_DELETES

    def plant(w: World) -> None:
        python_child(code, str(target), w.canary.relay_tokens[-1])

    world.plants["orders-key"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == ["profile-replaced:sys@orders-key"]
    assert not [leak for leak in report.leaks if "py-blind.txt" in leak[0]]  # the truncate went unseen


@pytest.mark.parametrize("which", ["sys", "threading"])
def test_the_exit_check_reports_a_hook_removed_during_the_run(tmp_path, tmp_path_factory, which):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    with recorder:
        recorder.set_stage("only")
        (sys.setprofile if which == "sys" else threading.setprofile)(None)
    recorder.finish()
    assert recorder.problems() == [f"profile-replaced:{which}@only"]
    assert sys.getprofile() is None and threading.getprofile() is None  # the outside world is restored


@pytest.mark.parametrize("how", ["r+", "os.open"])
def test_control_n_a_file_reopened_and_overwritten_in_place_is_read_at_the_open(make_world, how):
    world = make_world()
    target = world.home / "scratch" / "overwritten.txt"

    def plant(w: World) -> None:
        token = w.canary.relay_tokens[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token, encoding="utf-8")
        filler = "X" * len(token)
        if how == "r+":
            with open(target, "r+", encoding="utf-8") as handle:
                handle.write(filler)  # same length, in place: nothing is truncated
        else:
            descriptor = os.open(target, os.O_WRONLY)
            try:
                os.write(descriptor, filler.encode())
            finally:
                os.close(descriptor)
        target.unlink()

    world.plants["health"] = [plant]
    report = executed(world, run_flow_a)
    assert report.problems == []
    assert ("file-lost:home/scratch/overwritten.txt@health", "relay_token") in report.leaks
    assert report.leaks_where(lambda name: name.startswith("file:")) == []


def test_scrub_arm_response_strips_only_the_designed_values():
    canary = Canary.new()
    token = secrets.token_urlsafe(32)
    login_url = "https://login.example/?api_key=" + quote_plus(canary.app_key)
    clean = scrub_arm_response({"login_url": login_url, "relay_token": token, "attempt_id": "a"}, canary)
    assert quote_plus(canary.app_key) not in clean["login_url"] and token not in clean["relay_token"]
    assert clean["login_url"].startswith("https://login.example/?api_key=")
    dirty = scrub_arm_response(
        {"login_url": f"{login_url}&x={canary.api_session}", "relay_token": token + canary.secret_key}, canary
    )
    assert canary.api_session in dirty["login_url"] and canary.secret_key in dirty["relay_token"]


@pytest.mark.parametrize(
    "field, leaked",
    [("login_url", "api_session"), ("login_url", "secret_key"), ("relay_token", "api_session")],
)
def test_control_l_a_secret_added_to_an_arm_response_field_is_still_found(make_world, field, leaked):
    world = make_world()
    real_arm = SessionManager.arm

    def planted(self, presented):
        attempt_id, url, token, expires = real_arm(self, presented)
        extra = getattr(world.canary, leaked)
        if field == "login_url":
            url = f"{url}&x={extra}"
        else:
            token = token + extra
        return attempt_id, url, token, expires

    def flow(w: World) -> None:
        relay = w.new_client()
        with w.stage("arm"):
            try:
                relay.login_arm()
            except RelayError as err:  # the client refuses a malformed token; the response was captured already
                w.record_exc(err)

    with patch.object(SessionManager, "arm", planted):
        report = executed(world, flow)
    assert ("resp@arm", leaked) in report.leaks
    assert ("resp@arm", "app_key") not in report.leaks  # the designed AppKey is still allowed there


def test_temp_files_at_boundaries_reports_a_token_temp_file_left_behind():
    leftover = "file:home/.config/growin/.relay-session.json.1.deadbeef.tmp@x"
    report = Report(
        {
            leftover: b"",
            "file:home/.config/growin/relay-session.json@x": b"",  # the token file itself is not a temp file
            "file-lost:home/.config/growin/.relay-session.json.1.deadbeef.tmp@x": b"",  # a replaced temp: fine
            "file:home/.config/growin/other.tmp@x": b"",
        },
        {},
        [],
        [],
    )
    assert report.temp_files_at_boundaries() == [leftover]
    with pytest.raises(AssertionError, match="temp file"):
        report.assert_clean()
    assert Report({"file:home/.config/growin/relay-session.json@x": b""}, {}, [], []).temp_files_at_boundaries() == []


# -------------------------------------------------------- the recorder self-test


def test_control_e_the_recorder_sees_every_handled_event_on_this_interpreter(tmp_path, tmp_path_factory):
    """Proves each audit event fires here (CI runs 3.11): a renamed event fails loudly, not quietly."""
    capture = tmp_path_factory.mktemp("capture")
    recorder = FileRecorder([tmp_path], capture, tmp_path / "child-hook")

    def exercise(suffix: str) -> None:
        work = tmp_path / f"work-{suffix}"
        work.mkdir()
        (work / "trunc").write_text(f"PAY-open-{suffix}")
        with open(work / "trunc", "w", encoding="utf-8"):
            pass  # open event with a truncating write
        (work / "gone").write_text(f"PAY-remove-{suffix}")
        os.remove(work / "gone")
        (work / "src").write_text(f"PAY-rename-src-{suffix}")
        (work / "dst").write_text(f"PAY-rename-dst-{suffix}")
        os.replace(work / "src", work / "dst")
        (work / "lowopen").write_text(f"PAY-oswrite-{suffix}")
        os.close(os.open(work / "lowopen", os.O_WRONLY | os.O_TRUNC))
        (work / "viadirfd").write_text(f"PAY-dirfd-{suffix}")
        dirfd = os.open(work, os.O_RDONLY)
        try:
            os.remove("viadirfd", dir_fd=dirfd)  # a relative name: the recorder resolves it through the descriptor
        finally:
            os.close(dirfd)
        (work / "cut").write_text(f"PAY-truncate-{suffix}")
        os.truncate(work / "cut", 0)
        (work / "fdcut").write_text(f"PAY-ftruncate-{suffix}")
        fd = os.open(work / "fdcut", os.O_RDWR)
        try:
            os.ftruncate(fd, 0)
        finally:
            os.close(fd)
        with open(work / "ftrunc", "w", encoding="utf-8") as handle:
            handle.write(f"PAY-filetruncate-{suffix}")
            handle.truncate(0)  # no audit event: the profile hook
        (work / "emptydir").mkdir()
        os.rmdir(work / "emptydir")
        (work / "tree").mkdir()
        (work / "tree" / "leaf").write_text(f"PAY-rmtree-{suffix}")
        shutil.rmtree(work / "tree")
        subprocess.run(["/bin/sh", "-c", "true"], check=True)
        os.waitpid(os.posix_spawn("/bin/sh", ["sh", "-c", "true"], os.environ), 0)
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        try:
            pid, master = os.forkpty()
        except OSError:
            pass  # no pty here; the audit event fires before the call
        else:
            if pid == 0:
                os._exit(0)
            os.waitpid(pid, 0)
            os.close(master)

    with recorder:
        recorder.set_stage("selftest")
        exercise("on")
        events_on = len(recorder.records)
    exercise("off")  # the same calls with no active recorder
    assert len(recorder.records) == events_on, "the hook recorded something outside the with block"
    assert _Slot.recorder is None
    recorder.finish()

    expected = set(HookCore.HANDLED) | set(HookCore.PROFILED)
    assert recorder.seen_events() >= expected, expected - recorder.seen_events()
    lost = b"".join(recorder.lost_surfaces().values())
    for needle in (
        b"PAY-open-on",
        b"PAY-oswrite-on",
        b"PAY-dirfd-on",
        b"PAY-remove-on",
        b"PAY-rename-src-on",
        b"PAY-rename-dst-on",
        b"PAY-truncate-on",
        b"PAY-ftruncate-on",
        b"PAY-filetruncate-on",
        b"PAY-rmtree-on",
    ):
        assert needle in lost, needle
    assert b"-off" not in lost
    # Forking is the one thing the self-test does that fails a run, by design.
    assert recorder.problems() == ["child-unwatched:os.fork@selftest", "child-unwatched:os.forkpty@selftest"]
    assert recorder.native_children() == ["sh", "sh"]
    assert sys.version_info[:2] >= (3, 11)


def test_a_dir_fd_call_that_cannot_be_resolved_is_a_failure_not_a_skip(tmp_path, tmp_path_factory):
    recorder = FileRecorder([tmp_path], tmp_path_factory.mktemp("capture"), tmp_path / "child-hook")
    descriptor = os.open(tmp_path, os.O_RDONLY)
    os.close(descriptor)  # now a bad descriptor: the name cannot be placed under any root
    with recorder:
        with pytest.raises(OSError):
            os.remove("anything", dir_fd=descriptor)
    recorder.finish()
    assert [p.split(":")[0] for p in recorder.problems()] == ["file-unresolved"]


def test_the_capture_directory_is_never_inside_a_watched_root(make_world):
    world = make_world()
    capture = world.recorder.capture
    assert not _under(capture, Path(os.path.realpath(world.tmp)))
    assert not _under(capture, Path(os.path.realpath(world.home)))
    with pytest.raises(AssertionError):
        FileRecorder([world.tmp], world.tmp / "inside", world.tmp / "child-hook")


def test_the_child_hook_is_the_parent_hook_source(tmp_path, tmp_path_factory):
    source = child_sitecustomize_source()
    assert inspect.getsource(HookCore) in source
    compile(source, "sitecustomize.py", "exec")
