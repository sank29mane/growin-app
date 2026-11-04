#!/usr/bin/env python3
"""Value-based secret scanner for the Mac and the VM (GATE-03, plan 61-11 Task 2).

    python gateway/ops/scan_secrets.py mac     # the operator's Mac, on the login day
    sudo python3 scan_secrets.py vm            # the VM, as root

It searches for the real values (AppKey, secret key, broker user id, relay token, API_Session code)
in raw, URL-encoded, base64 and JSON-escaped form, and on runtime targets also for the shapes those
secrets take in headers and URLs. It prints PASS, FAIL or INCOMPLETE lines with counts and file paths
only. It never prints a matched line, a value or any form of one.

    PASS        every promised target was read to the end and nothing was found
    FAIL        something was found (exit 1); FAIL beats INCOMPLETE
    INCOMPLETE  nothing found, but a promised file, directory, command or secret class was skipped,
                unreadable or not fully read (exit 2). Never a pass.

Repo targets (the working tree, git history) are searched by value only: tracked test fixtures carry
the header shapes by design. Runtime targets (journal, /var/log, /tmp, shell histories, ...) get value
and shape. Standard library only, Python 3.11 to 3.14, no import of gateway_vm.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, quote_plus

CHUNK = 1 << 20
MIN_FORM = 8
USER_ID_RAW_MIN = 5  # the relay accepts 3 to 32 characters; 5 to 7 are searched as a bounded regex
RULE_SKIP_DIRS = (".git", ".venv", "node_modules", "__pycache__")
SDK_REMNANTS = ("apiLogs.log", "websocketLogs.log")
COMMAND_TIMEOUT_S = 1800

EXIT_PASS, EXIT_FAIL, EXIT_INCOMPLETE, EXIT_REFUSED = 0, 1, 2, 3

# Classes. Values come from prompts, the token file, relay.json or Secret Manager.
APP_KEY, SECRET_KEY, USER_ID = "app_key", "secret_key", "user_id"
RELAY_TOKEN, SESSION_CODE = "relay_token", "session_code"
SESSION_TOKEN, SDK_HEADER, SDK_REMNANT = "session_token", "sdk_header", "sdk_remnant"

REPO_REQUIRED = (APP_KEY, SECRET_KEY, USER_ID, RELAY_TOKEN)
RUNTIME_REQUIRED = (APP_KEY, SECRET_KEY, USER_ID, SESSION_TOKEN, RELAY_TOKEN, SESSION_CODE, SDK_HEADER)

# --------------------------------------------------------------------------- shapes
# Every shape is bounded: no `*`, `+` or open `{n,}`, whitespace runs capped at {0,8}. MAX_MATCH is
# the widest match the pattern can make; the tests check it against the compiled pattern and size the
# chunk overlap from it (plus one byte for a one-character lookaround).
_B64URL = rb"[A-Za-z0-9_-]"
SHAPES: tuple[tuple[str, str, bytes, int], ...] = (
    # (class, name, pattern, MAX_MATCH)
    (
        RELAY_TOKEN,
        "bearer",
        rb'authorization"{0,1}[ \t]{0,8}[:=][ \t]{0,8}"{0,1}bearer[ \t]{1,8}' + _B64URL + rb"{43}(?!" + _B64URL + rb")",
        13 + 1 + 8 + 1 + 8 + 1 + 6 + 8 + 43,
    ),
    (
        RELAY_TOKEN,
        "relay_token_json",
        rb'"relay_token"[ \t]{0,8}:[ \t]{0,8}"' + _B64URL + rb"{43}(?!" + _B64URL + rb")",
        13 + 8 + 1 + 8 + 1 + 43,
    ),
    (
        SESSION_CODE,
        "api_session",
        # The relay's own contract is ^[A-Za-z0-9]{6,64}$ (contract.py API_SESSION_RE). At least one digit is
        # required so source like `api_session=apisession` is not a hit; quotes and `%3D` are allowed.
        # A quote is a plain quote, its URL-encoded form (%22, %27) or a backslash-escaped one (JSON in a string).
        # Residuals outside the plan's shapes: double encoding (%253D) and URL-encoded JSON (apisession%22%3A%22...).
        rb"(?:apisession|api_session)(?:[\x27\"]|%2[27]|\\[\x27\"]){0,1}[ \t]{0,8}(?:[=:]|%3d)[ \t]{0,8}"
        rb"(?:[\x27\"]|%2[27]|\\[\x27\"]){0,1}(?=[A-Za-z]{0,63}[0-9])[A-Za-z0-9]{6,64}(?![A-Za-z0-9_])",
        11 + 3 + 8 + 3 + 8 + 3 + 64,
    ),
    (
        SDK_HEADER,
        "sdk_header",
        rb'x-(?:sessiontoken|checksum)"{0,1}[ \t]{0,8}[:=][ \t]{0,8}"{0,1}(?:token[ \t]{1,8}){0,1}[A-Za-z0-9+/=]{8,64}',
        2 + 12 + 1 + 8 + 1 + 8 + 1 + 5 + 8 + 64,
    ),
)
# A base64 run that decodes to "<user id>:<digits>" is the Breeze session token.
TOKEN_RUN = rb"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{16,128}={0,2}(?![A-Za-z0-9+/=])"
TOKEN_RUN_MAX_MATCH = 128 + 2
_SHAPE_RES = tuple((cls, re.compile(pat, re.IGNORECASE)) for cls, _, pat, _ in SHAPES)
_TOKEN_RUN_RE = re.compile(TOKEN_RUN)
MAX_SHAPE_MATCH = max([m for *_, m in SHAPES] + [TOKEN_RUN_MAX_MATCH])
COMMIT_RE = re.compile(rb"^commit [0-9a-f]{40}", re.MULTILINE)
COMMIT_MAX_MATCH = 47


# --------------------------------------------------------------------------- forms


def expand_forms(value: str) -> list[str]:
    """Raw, quote_plus, quote(safe=""), standard base64 and the JSON-escaped form; de-duplicated,
    forms shorter than eight characters dropped."""
    if not isinstance(value, str) or not value:
        return []
    candidates = [
        value,
        quote_plus(value),
        quote(value, safe=""),
        base64.b64encode(value.encode("utf-8")).decode("ascii"),
        json.dumps(value)[1:-1],
    ]
    seen: list[str] = []
    for form in candidates:
        if len(form) >= MIN_FORM and form not in seen:
            seen.append(form)
    return seen


def _is_session_token(run: bytes, user_id: bytes) -> bool:
    head_len = ((len(user_id) + 1 + 2) // 3) * 4
    run = run.rstrip(b"=")
    if len(run) < head_len:
        return False
    try:
        head = base64.b64decode(run[:head_len])
    except (binascii.Error, ValueError):
        return False
    if not head.startswith(user_id + b":"):
        return False
    try:
        body = base64.b64decode(run + b"=" * (-len(run) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return body[len(user_id) + 1 :].isdigit()


class Spec:
    """What to look for: value forms per class, and whether to look for shapes too.

    `skip` drops classes entirely (a per-path exclusion). A class with neither forms nor a shape is
    reported by `uncovered`, never silently treated as clean.
    """

    def __init__(
        self,
        values: Mapping[str, str | None],
        *,
        shapes: bool = False,
        skip: frozenset[str] = frozenset(),
    ) -> None:
        self.values = {k: v for k, v in values.items() if v}
        self.shapes = shapes
        self.skip = skip
        self.forms: dict[str, tuple[bytes, ...]] = {}
        # A value under the floor loses its raw form (only its longer encodings are searched), so a plain
        # occurrence would pass. Such a class is never "covered": the target reports INCOMPLETE.
        self.short = frozenset(
            cls for cls, value in self.values.items() if len(value) < (USER_ID_RAW_MIN if cls == USER_ID else MIN_FORM)
        )
        for cls, value in self.values.items():
            forms = tuple(f.encode("utf-8") for f in expand_forms(value))
            if forms and cls not in skip:
                self.forms[cls] = forms
        user = self.values.get(USER_ID)
        self._user_bytes = user.encode("utf-8") if user else None
        # A user id of 5 to 7 characters has no raw form in `forms` (floor 8). It is also searched as itself,
        # case-insensitively, with alphanumeric boundaries so a longer word that contains it is not a hit.
        self.raw_res: dict[str, re.Pattern[bytes]] = {}
        if user and USER_ID_RAW_MIN <= len(user) < MIN_FORM and USER_ID not in skip:
            self.raw_res[USER_ID] = re.compile(
                rb"(?<![A-Za-z0-9])" + re.escape(user.encode("utf-8")) + rb"(?![A-Za-z0-9])", re.IGNORECASE
            )
        self.longest_raw = len(user.encode("utf-8")) if self.raw_res and user else 0
        self.shape_res = tuple((c, r) for c, r in _SHAPE_RES if c not in skip) if shapes else ()
        self.longest_form = max((len(f) for forms in self.forms.values() for f in forms), default=0)
        self.longest_shape = MAX_SHAPE_MATCH if shapes else 0

    @property
    def overlap(self) -> int:
        """Bytes carried between windows: the widest thing that can match, plus one for lookarounds."""
        return max(self.longest_form, self.longest_raw, self.longest_shape, COMMIT_MAX_MATCH) + 1

    def restrict(self, skip: frozenset[str]) -> Spec:
        return Spec(self.values, shapes=self.shapes, skip=self.skip | skip)

    def covered(self, cls: str) -> bool:
        if cls == SESSION_CODE and cls in self.short:
            return self.shapes  # a short code is treated as no value entered: the shapes cover it, or nothing does
        if cls in self.short:
            return False
        if cls in self.raw_res or cls in self.forms:
            return True
        if not self.shapes:
            return False
        if cls == SESSION_TOKEN:
            return self._user_bytes is not None
        return cls in {c for c, *_ in SHAPES}

    def uncovered(self, required: Iterable[str]) -> list[str]:
        return [cls for cls in required if not self.covered(cls)]

    def search(
        self,
        window: bytes,
        already: set[str] | None = None,
        *,
        start: int = 0,
        final: bool = True,
        deferred: set[str] | None = None,
    ) -> set[str]:
        """Classes found in `window`. Every regex class (the raw short user id, the shapes, the session
        token run) judges its lookarounds against the real neighbours, not the window edges: `start=1`
        lets the byte before the first carried byte serve as lookbehind context, and unless `final` a
        match ending on the window's last byte is left to the next window, which sees the byte after
        it. Those classes go into `deferred` (when given) for the caller to accept at end of stream."""
        found: set[str] = set()
        pending: set[str] = set()
        done = already or set()
        for cls, forms in self.forms.items():
            if cls in done:
                continue
            for form in forms:
                if form in window:
                    found.add(cls)
                    break
        end = len(window)
        for cls, regex in (*self.raw_res.items(), *self.shape_res):
            if cls in done or cls in found:
                continue
            for m in regex.finditer(window, start):
                if final or m.end() < end:
                    found.add(cls)
                    break
                pending.add(cls)
        if (
            self.shapes
            and self._user_bytes is not None
            and SESSION_TOKEN not in self.skip
            and SESSION_TOKEN not in done
            and SESSION_TOKEN not in found
        ):
            for m in _TOKEN_RUN_RE.finditer(window, start):
                if _is_session_token(m.group(0), self._user_bytes):
                    if final or m.end() < end:
                        found.add(SESSION_TOKEN)
                        break
                    pending.add(SESSION_TOKEN)
        if deferred is not None:
            deferred |= pending - found
        return found


# --------------------------------------------------------------------------- streaming


@dataclass
class Outcome:
    hits: set[str] = field(default_factory=set)
    partial: bool = False
    commits: int = 0


def scan_chunks(
    chunks: Iterable[bytes],
    spec: Spec,
    *,
    max_bytes: int | None = None,
    count_commits: bool = False,
) -> Outcome:
    """Search a byte stream window by window. Each window is the previous window's last `overlap`
    bytes plus the next chunk, so a match across a chunk edge is found. No size cap: `max_bytes` is a
    test hook, and any stream cut short by it is `partial`."""
    out = Outcome()
    overlap = spec.overlap
    carry = b""
    trimmed = False  # True once the carry is a proper suffix: its first byte has a real left neighbour
    deferred: set[str] = set()  # regex matches ending on the last window byte, pending the next window
    total = 0
    try:
        for chunk in chunks:
            if not chunk:
                continue
            if max_bytes is not None:
                room = max_bytes - total
                if room <= 0:
                    out.partial = True
                    break
                if len(chunk) > room:
                    chunk = chunk[:room]
                    out.partial = True
            total += len(chunk)
            window = carry + chunk
            start = 1 if trimmed else 0
            deferred = set()
            out.hits |= spec.search(window, out.hits, start=start, final=False, deferred=deferred)
            if count_commits:
                # a header wholly inside the carried bytes was counted with the previous window
                out.commits += sum(1 for m in COMMIT_RE.finditer(window) if m.end() > len(carry))
            trimmed = trimmed or len(window) > overlap
            carry = window[-overlap:]
            if out.partial:
                break
        if not out.partial:  # a cut-short stream (`max_bytes`) ends on an artificial edge: it rules nothing in
            out.hits |= deferred  # the stream ended on that match: nothing follows it to rule it out
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()  # stops a still-running command when a partial read ends the scan early
    return out


def scan_text(data: bytes | str, spec: Spec) -> set[str]:
    """The classes found in one in-memory text (a journal dump, a path)."""
    raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
    return scan_chunks([raw], spec).hits


def _read_chunks(handle, size: int) -> Iterator[bytes]:
    while True:
        chunk = handle.read(size)
        if not chunk:
            return
        yield chunk


# --------------------------------------------------------------------------- reports


@dataclass
class Report:
    label: str
    required: bool = True
    files: int = 0
    specials: int = 0
    symlinks: int = 0
    unreadable: int = 0
    partial: int = 0
    absent: int = 0
    commits: int | None = None
    missing: bool = False
    failed: str | None = None
    uncovered: list[str] = field(default_factory=list)
    rule_skipped: set[str] = field(default_factory=set)
    hits: dict[str, set[str]] = field(default_factory=dict)

    @property
    def all_absent(self) -> bool:
        return self.files == 0 and self.absent > 0 and not self.hits and not self.missing

    @property
    def status(self) -> str:
        if self.hits:
            return "FAIL"
        if not self.required and self.all_absent:
            return "PASS"
        if self.missing or self.failed or self.unreadable or self.partial or self.uncovered:
            return "INCOMPLETE"
        return "PASS"

    def lines(self) -> list[str]:
        if self.status == "PASS" and self.all_absent and not self.required:
            return [f"absent {self.label}"]
        parts = [f"{self.status} {self.label}"]
        if self.status == "FAIL":
            parts.append(f"hits={len(self.hits)}")
        if self.status == "INCOMPLETE":
            if self.missing:
                parts.append("missing")
            if self.failed:
                parts.append(f"failed={self.failed}")
            if self.uncovered:
                parts.append("uncovered=" + ",".join(self.uncovered))
            parts.append(f"unreadable={self.unreadable}")
            parts.append(f"partial={self.partial}")
        if self.status in ("PASS", "FAIL") and self.commits is None:
            parts.append(f"files={self.files}")
        if self.commits is not None:
            parts.append(f"commits={self.commits}")
        if self.specials:
            parts.append(f"specials={self.specials}")
        if self.symlinks:
            parts.append(f"symlinks={self.symlinks}")
        if self.absent and not self.missing:
            parts.append(f"absent={self.absent}")
        if self.rule_skipped:
            parts.append("skipped-by-rule=" + ",".join(sorted(self.rule_skipped)))
        out = [" ".join(parts)]
        for path in sorted(self.hits):
            out.append(f"  hit {path} {','.join(sorted(self.hits[path]))}")
        return out


# --------------------------------------------------------------------------- files and trees


@dataclass(frozen=True)
class Exclusions:
    """Per-path class exclusions. The user-id class skips the two places that hold it by design. The
    relay-token value is skipped only at the token file and its `.relay-session.json.*.tmp` siblings."""

    user_id_dirs: tuple[Path, ...] = ()
    token_path: Path | None = None

    def classes_for(self, path: Path) -> frozenset[str]:
        skip: set[str] = set()
        if any(path == d or d in path.parents for d in self.user_id_dirs):
            skip.add(USER_ID)
        token = self.token_path
        if token is not None and path.parent == token.parent:
            tmp = re.fullmatch(r"\." + re.escape(token.name) + r"\.\d+\.[0-9a-f]{8}\.tmp", path.name)
            if path == token or tmp:
                skip.add(RELAY_TOKEN)
        return frozenset(skip)


class _SpecCache:
    def __init__(self, spec: Spec) -> None:
        self.base = spec
        self._cache: dict[frozenset[str], Spec] = {}

    def get(self, skip: frozenset[str]) -> Spec:
        if not skip:
            return self.base
        if skip not in self._cache:
            self._cache[skip] = self.base.restrict(skip)
        return self._cache[skip]


_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def _safe_name(path_text: str, spec: Spec, index: int) -> str:
    """A path is printed only if it does not itself carry a value. Values, the full runtime shape set and
    every known raw value (whatever the detection floor) redact it; control characters are escaped."""
    lowered = path_text.lower()
    if (
        scan_text(path_text, Spec(spec.values, shapes=True))
        or any(value.lower() in lowered for value in spec.values.values())
    ):
        return f"<redacted-path-{index}>"
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", path_text)


def _scan_one(path: Path, spec: Spec, report: Report, shown: str, *, max_bytes, chunk_size) -> None:
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed in the finally below
    except OSError:
        report.unreadable += 1
        return
    try:
        out = scan_chunks(_read_chunks(handle, chunk_size), spec, max_bytes=max_bytes)
    except OSError:
        report.unreadable += 1
        return
    finally:
        handle.close()
    report.files += 1
    if out.partial:
        report.partial += 1
    if out.hits:
        report.hits.setdefault(shown, set()).update(out.hits)


def _walk(root: Path, report: Report) -> Iterator[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            report.unreadable += 1
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    report.symlinks += 1
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name in RULE_SKIP_DIRS:
                        report.rule_skipped.add(entry.name)
                    else:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
                else:  # socket, FIFO, device: reading a FIFO can block
                    report.specials += 1
            except OSError:
                report.unreadable += 1


def scan_tree(
    root: Path | str,
    spec: Spec,
    *,
    label: str = "tree",
    exclusions: Exclusions | None = None,
    max_bytes: int | None = None,
    chunk_size: int = CHUNK,
    required: bool = True,
) -> Report:
    """Search every regular file under root, paths reported relative to it. Symlinks are not followed."""
    root = Path(root)
    report = Report(label, required=required)
    if not root.is_dir():
        report.missing = required
        report.absent = 0 if required else 1
        return report
    cache = _SpecCache(spec)
    exclusions = exclusions or Exclusions()
    index = 0
    for path in _walk(root, report):
        index += 1
        rel = path.relative_to(root).as_posix()
        shown = _safe_name(rel, spec, index)
        _scan_one(
            path, cache.get(exclusions.classes_for(path)), report, shown, max_bytes=max_bytes, chunk_size=chunk_size
        )
        if path.name in SDK_REMNANTS:
            report.hits.setdefault(shown, set()).add(SDK_REMNANT)
    return report


def scan_files(
    paths: Sequence[Path | str],
    spec: Spec,
    *,
    label: str = "files",
    display: Callable[[Path], str] | None = None,
    exclusions: Exclusions | None = None,
    max_bytes: int | None = None,
    chunk_size: int = CHUNK,
    required: bool = False,
) -> Report:
    """Search named files (shell histories). A missing file is `absent`, never a pass about it when required."""
    report = Report(label, required=required)
    cache = _SpecCache(spec)
    exclusions = exclusions or Exclusions()
    for index, raw in enumerate(paths, start=1):
        path = Path(raw)
        if not os.path.lexists(path):
            report.absent += 1
            if required:
                report.missing = True
            continue
        if path.is_symlink():
            report.symlinks += 1
            continue
        if not path.is_file():
            report.specials += 1
            continue
        shown = _safe_name(display(path) if display else str(path), spec, index)
        _scan_one(
            path, cache.get(exclusions.classes_for(path)), report, shown, max_bytes=max_bytes, chunk_size=chunk_size
        )
        if path.name in SDK_REMNANTS:
            report.hits.setdefault(shown, set()).add(SDK_REMNANT)
    return report


# --------------------------------------------------------------------------- commands


class CommandFailed(Exception):
    """A subprocess exited non-zero, timed out or could not start. No output is kept."""


def stream_command(argv: Sequence[str], cwd: Path | None = None, timeout: int = COMMAND_TIMEOUT_S) -> Iterator[bytes]:
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        raise CommandFailed from None
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    finished = False
    try:
        assert proc.stdout is not None
        yield from _read_chunks(proc.stdout, CHUNK)
        finished = True
    finally:
        timer.cancel()
        if not finished:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    if timed_out.is_set() or proc.returncode != 0:
        raise CommandFailed


GitRunner = Callable[[Sequence[str], Path], Iterable[bytes]]
JournalReader = Callable[[str | None], Iterable[bytes]]


def default_run_git(args: Sequence[str], cwd: Path) -> Iterable[bytes]:
    return stream_command(["git", *args], cwd=cwd)


def default_read_journal(unit: str | None) -> Iterable[bytes]:
    argv = ["journalctl", "--no-pager", "-o", "cat", "--since", "30 days ago"]
    if unit:
        argv += ["-u", unit]
    return stream_command(argv)


def scan_git_history(
    repo: Path | str,
    spec: Spec,
    *,
    run_git: GitRunner = default_run_git,
    label: str = "git-history",
    max_bytes: int | None = None,
) -> Report:
    """`git log -p --all --no-color --text`, streamed (`--text` so binary diffs are searched too). A command that fails leaves the target INCOMPLETE."""
    report = Report(label)
    try:
        out = scan_chunks(
            run_git(["log", "-p", "--all", "--no-color", "--text"], Path(repo)), spec, max_bytes=max_bytes, count_commits=True
        )
    except Exception:  # noqa: BLE001 - a failed or injected-failing runner is an incomplete target, never a pass
        report.failed = "git"
        return report
    report.commits = out.commits
    if out.partial:
        report.partial += 1
    for cls in out.hits:
        report.hits[f"history:{cls}"] = {cls}
    return report


def scan_journal(
    label: str, unit: str | None, spec: Spec, *, read_journal: JournalReader, max_bytes: int | None = None
) -> Report:
    report = Report(label)
    try:
        out = scan_chunks(read_journal(unit), spec, max_bytes=max_bytes)
    except Exception:  # noqa: BLE001 - a journal that cannot be read to the end is INCOMPLETE
        report.failed = "journal"
        return report
    report.files = 1
    if out.partial:
        report.partial += 1
    for cls in out.hits:
        report.hits[f"journal:{cls}"] = {cls}
    return report


# --------------------------------------------------------------------------- value sources


def default_token_path(home: Path) -> Path:
    return home / ".config" / "growin" / "relay-session.json"


def read_relay_token(path: Path) -> str | None:
    """The `token` field of the Mac token file, read without printing. Anything else is None."""
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8"))
        token = data["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) else None


def _default_prompts(name: str) -> str:
    text = {
        APP_KEY: "AppKey: ",
        SECRET_KEY: "Secret key: ",
        USER_ID: "ICICI user id: ",
        SESSION_CODE: "Today's API_Session code (Enter to skip): ",
    }[name]
    return getpass.getpass(text)


def _http_get(url: str, headers: Mapping[str, str]) -> bytes:
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310
    with opener.open(request, timeout=10) as response:  # noqa: S310
        return response.read(1 << 16)


def default_fetch_secrets(names: Mapping[str, str]) -> dict[str, str]:
    """The two Breeze secrets from Secret Manager, through the VM's metadata token (REST shapes of
    gateway_vm/secret_source.py, reimplemented because this script runs standalone)."""
    meta = "http://metadata.google.internal/computeMetadata/v1/"
    flavor = {"Metadata-Flavor": "Google"}
    project = _http_get(meta + "project/project-id", flavor).decode("ascii").strip()
    access = json.loads(_http_get(meta + "instance/service-accounts/default/token", flavor))["access_token"]
    base = f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/"
    result: dict[str, str] = {}
    for cls, key in ((APP_KEY, "app_key"), (SECRET_KEY, "secret_key")):
        body = _http_get(f"{base}{names[key]}/versions/latest:access", {"Authorization": f"Bearer {access}"})
        result[cls] = base64.b64decode(json.loads(body)["payload"]["data"], validate=True).decode("ascii")
    return result


# --------------------------------------------------------------------------- modes

REPO_ROOT = Path(__file__).resolve().parents[2]
VM_REQUIRED_ROOTS = ("/etc", "/etc/growin-gateway", "/var/lib/private/growin-gateway", "/opt/growin-gateway")
VM_OPTIONAL_ROOTS = ("/var/log", "/tmp", "/var/tmp", "/run", "/home", "/root")
VM_USER_ID_DIRS = ("/etc/growin-gateway", "/run/credentials")


def _label(path: str) -> str:
    return path.strip("/").replace("/", "-") or "root"


def _finish(reports: list[Report], notes: list[str]) -> int:
    for report in reports:
        for line in report.lines():
            print(line)
    for note in notes:
        print(note)
    statuses = {r.status for r in reports}
    result = "FAIL" if "FAIL" in statuses else "INCOMPLETE" if "INCOMPLETE" in statuses else "PASS"
    print(f"RESULT {result}")
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "INCOMPLETE": EXIT_INCOMPLETE}[result]


def run_mac(
    *,
    prompts: Callable[[str], str] | None,
    run_git: GitRunner,
    token_path: Path | None,
    repo_root: Path,
    home: Path,
    max_bytes: int | None,
) -> int:
    ask = prompts or _default_prompts
    values: dict[str, str | None] = {
        APP_KEY: ask(APP_KEY),
        SECRET_KEY: ask(SECRET_KEY),
        USER_ID: ask(USER_ID),
        SESSION_CODE: ask(SESSION_CODE),
    }
    token = token_path if token_path is not None else default_token_path(home)
    values[RELAY_TOKEN] = read_relay_token(token)
    try:
        repo_spec = Spec(values, shapes=False)  # repo targets: value only (tracked fixtures carry shapes)
        runtime_spec = Spec(values, shapes=True)
        notes: list[str] = []
        if not repo_spec.covered(SESSION_CODE):
            why = "value under 8 characters" if SESSION_CODE in repo_spec.short else "no value entered"
            notes.append(f"uncovered: session_code ({why})")
        if USER_ID in repo_spec.short:
            notes.append(f"uncovered: user_id (value under {USER_ID_RAW_MIN} characters)")
        repo_gaps = repo_spec.uncovered(REPO_REQUIRED)
        runtime_gaps = runtime_spec.uncovered(RUNTIME_REQUIRED)
        repo_excl = Exclusions(
            user_id_dirs=(repo_root / "private", repo_root / "docs" / "icici-docs" / "_local"), token_path=token
        )
        run_excl = Exclusions(token_path=token)
        reports: list[Report] = []

        tree = scan_tree(repo_root, repo_spec, label="working-tree", exclusions=repo_excl, max_bytes=max_bytes)
        tree.uncovered = list(repo_gaps)
        reports.append(tree)
        history = scan_git_history(repo_root, repo_spec, run_git=run_git, max_bytes=max_bytes)
        history.uncovered = list(repo_gaps)
        reports.append(history)

        shell = scan_files(
            [home / ".zsh_history", home / ".bash_history"],
            runtime_spec,
            label="shell-history",
            display=lambda p: "~/" + p.relative_to(home).as_posix(),
            exclusions=run_excl,
            max_bytes=max_bytes,
        )
        reports.append(shell)
        for rel, label in (((".config", "growin"), "growin-config"), ((".config", "gcloud", "logs"), "gcloud-logs")):
            target = scan_tree(
                home.joinpath(*rel), runtime_spec, label=label, exclusions=run_excl, max_bytes=max_bytes, required=False
            )
            reports.append(target)
        for report in reports[2:]:
            report.uncovered = list(runtime_gaps)
        return _finish(reports, notes)
    finally:
        values.clear()


def run_vm(
    *,
    fetch_secrets: Callable[[Mapping[str, str]], Mapping[str, str]],
    read_journal: JournalReader,
    fs_root: Path,
    max_bytes: int | None,
) -> int:
    def at(path: str) -> Path:
        return fs_root / path.lstrip("/")

    reports: list[Report] = []
    values: dict[str, str | None] = {}
    names: Mapping[str, str] | None = None
    try:
        try:
            config = json.loads(at("/etc/growin-gateway/relay.json").read_bytes().decode("utf-8"))
            values[USER_ID] = config["expected_userid"]
            names = config["secret_names"]
        except (OSError, ValueError, KeyError, TypeError):
            reports.append(Report("relay-config", failed="relay.json"))
        if names is not None:
            try:
                fetched = fetch_secrets(names)
                values[APP_KEY] = fetched[APP_KEY]
                values[SECRET_KEY] = fetched[SECRET_KEY]
            except Exception:  # noqa: BLE001 - no value, no PASS: the classes show as uncovered
                reports.append(Report("secret-manager", failed="fetch"))
        spec = Spec(values, shapes=True)
        gaps = spec.uncovered(RUNTIME_REQUIRED)
        excl = Exclusions(user_id_dirs=tuple(at(p) for p in VM_USER_ID_DIRS))

        journal = scan_journal("journal", None, spec, read_journal=read_journal, max_bytes=max_bytes)
        unit = scan_journal("unit-journal", "growin-gateway", spec, read_journal=read_journal, max_bytes=max_bytes)
        for report in (journal, unit):
            report.uncovered = list(gaps)
            reports.append(report)
        for path in VM_REQUIRED_ROOTS + VM_OPTIONAL_ROOTS:
            required = path in VM_REQUIRED_ROOTS
            report = scan_tree(
                at(path), spec, label=_label(path), exclusions=excl, max_bytes=max_bytes, required=required
            )
            report.uncovered = list(gaps)
            reports.append(report)
        notes = [f"uncovered: user_id (value under {USER_ID_RAW_MIN} characters)"] if USER_ID in spec.short else []
        return _finish(reports, notes)
    finally:
        values.clear()


def main(
    argv: Sequence[str] | None = None,
    *,
    prompts: Callable[[str], str] | None = None,
    fetch_secrets: Callable[[Mapping[str, str]], Mapping[str, str]] | None = None,
    read_journal: JournalReader | None = None,
    run_git: GitRunner | None = None,
    token_path: Path | None = None,
    geteuid: Callable[[], int] = os.geteuid,
    repo_root: Path | None = None,
    home: Path | None = None,
    fs_root: Path | None = None,
    max_bytes: int | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("mac", "vm"):
        print("usage: scan_secrets.py mac|vm")
        return EXIT_REFUSED
    if args[0] == "mac":
        if prompts is None and not sys.stdin.isatty():
            print("REFUSED mac mode needs a terminal for the hidden prompts")
            return EXIT_REFUSED
        return run_mac(
            prompts=prompts,
            run_git=run_git or default_run_git,
            token_path=token_path,
            repo_root=repo_root or REPO_ROOT,
            home=home or Path.home(),
            max_bytes=max_bytes,
        )
    if geteuid() != 0:
        print("REFUSED vm mode must run as root")
        return EXIT_REFUSED
    return run_vm(
        fetch_secrets=fetch_secrets or default_fetch_secrets,
        read_journal=read_journal or default_read_journal,
        fs_root=fs_root or Path("/"),
        max_bytes=max_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
