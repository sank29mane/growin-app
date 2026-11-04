"""The value-based secret scanner (Phase 61-11 Task 2, GATE-03; rows D6, D11, D15 to D17, D19 to D21, D23).

Every fake is generated at run time from os.urandom, so no secret-shaped literal lives in this file.
Planted controls prove the scanner finds a fake in every encoding and target class; negative controls
prove it never says PASS about something it did not read and never prints what it found.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import re
import re._constants as re_constants
import re._parser as re_parser
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "gateway" / "ops" / "scan_secrets.py"
_spec = importlib.util.spec_from_file_location("scan_secrets_under_test", SCRIPT)
scan = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scan
_spec.loader.exec_module(scan)

MiB = 1 << 20


def _digits(n: int) -> str:
    return "".join(str(b % 10) for b in os.urandom(n))


class Fakes:
    """Random fakes. app_key carries characters that every encoding changes."""

    def __init__(self) -> None:
        self.app_key = f"ak/{secrets.token_hex(6)}+q&\"z"
        self.secret_key = f"sk{secrets.token_hex(8)}"
        self.user_id = "FK" + secrets.token_hex(4).upper()
        self.relay_token = secrets.token_urlsafe(32)  # 43 characters of [A-Za-z0-9_-]
        self.session_code = _digits(8)
        self.breeze_token = base64.b64encode(f"{self.user_id}:{_digits(8)}".encode()).decode()

    @property
    def values(self) -> dict[str, str]:
        return {
            scan.APP_KEY: self.app_key,
            scan.SECRET_KEY: self.secret_key,
            scan.USER_ID: self.user_id,
            scan.RELAY_TOKEN: self.relay_token,
            scan.SESSION_CODE: self.session_code,
        }

    def prompts(self, name: str) -> str:
        return {
            scan.APP_KEY: self.app_key,
            scan.SECRET_KEY: self.secret_key,
            scan.USER_ID: self.user_id,
            scan.SESSION_CODE: self.session_code,
        }[name]


@pytest.fixture()
def fakes() -> Fakes:
    return Fakes()


def run_scan(capsys, argv, **kwargs):
    code = scan.main(argv, **kwargs)
    return code, capsys.readouterr().out


# ------------------------------------------------------------- expand_forms


def test_expand_forms_lists_every_encoding_once(fakes):
    forms = scan.expand_forms(fakes.app_key)
    expected = [
        fakes.app_key,
        quote_plus(fakes.app_key),
        quote(fakes.app_key, safe=""),  # same as quote_plus here: no space in the value, so deduped
        base64.b64encode(fakes.app_key.encode()).decode(),
        json.dumps(fakes.app_key)[1:-1],
    ]
    assert forms == list(dict.fromkeys(expected))
    assert len(forms) == 4
    spaced = scan.expand_forms("a b/c+d" + "e" * 6)
    assert quote_plus("a b/c+d" + "e" * 6) in spaced and quote("a b/c+d" + "e" * 6, safe="") in spaced


def test_expand_forms_dedupes_and_drops_short_forms():
    plain = "abcdefgh1234"
    forms = scan.expand_forms(plain)
    assert forms == [plain, base64.b64encode(plain.encode()).decode()]
    assert scan.expand_forms("abc") == []  # every form is under eight characters
    assert scan.expand_forms("") == []


# ------------------------------------------------------------- scan_tree


def _plant_forms(tree: Path, value: str) -> dict[str, str]:
    forms = scan.expand_forms(value)
    names = {}
    for i, form in enumerate(forms):
        rel = f"sub/f{i}.txt"
        (tree / "sub").mkdir(exist_ok=True)
        (tree / rel).write_text(f"before {form} after\n")
        names[rel] = form
    return names


def test_scan_tree_finds_every_encoding_and_reports_relative_paths(tmp_path, fakes):
    planted = _plant_forms(tmp_path, fakes.app_key)
    for skipped in (".git", ".venv", "node_modules", "__pycache__"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "x.txt").write_text(fakes.app_key)
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    assert set(report.hits) == set(planted)
    assert report.status == "FAIL"
    assert report.rule_skipped == {".git", ".venv", "node_modules", "__pycache__"}
    line = report.lines()[0]
    assert "skipped-by-rule=.git,.venv,__pycache__,node_modules" in line
    assert not any(str(tmp_path) in ln for ln in report.lines())


def test_scan_tree_clean_tree_passes(tmp_path, fakes):
    (tmp_path / "a.txt").write_text("nothing here")
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    assert (report.status, report.files, report.hits) == ("PASS", 1, {})


def test_symlinks_and_special_files_are_counted_not_read(tmp_path, fakes):
    secret_file = tmp_path / "real.txt"
    secret_file.write_text("clean")
    (tmp_path / "link.txt").symlink_to(secret_file)
    os.mkfifo(tmp_path / "pipe")
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    assert (report.symlinks, report.specials, report.files) == (1, 1, 1)
    assert report.status == "PASS"


def test_sdk_remnant_file_is_a_fail(tmp_path, fakes):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "apiLogs.log").write_text("empty")
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    assert report.status == "FAIL" and report.hits == {"logs/apiLogs.log": {scan.SDK_REMNANT}}
    (tmp_path / "websocketLogs.log").write_text("")
    assert set(scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t").hits) == {
        "logs/apiLogs.log",
        "websocketLogs.log",
    }


def test_a_path_that_carries_a_secret_is_never_printed(tmp_path, fakes):
    name = f"{fakes.secret_key}.txt"
    (tmp_path / name).write_text(fakes.secret_key)
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    text = "\n".join(report.lines())
    assert fakes.secret_key not in text
    assert "<redacted-path-" in text


# ------------------------------------------------- D16 oversize, D15 completeness


def test_d16_a_fake_past_51_mib_is_found(tmp_path, fakes):
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:
        handle.seek(51 * MiB)
        handle.write(fakes.app_key.encode())
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    assert report.status == "FAIL" and set(report.hits) == {"big.bin"}


def test_d15_b_a_forced_partial_read_is_incomplete_never_pass(tmp_path, fakes):
    big = tmp_path / "big.bin"
    with open(big, "wb") as handle:
        handle.seek(51 * MiB)
        handle.write(fakes.app_key.encode())
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t", max_bytes=1 * MiB)
    assert report.partial == 1 and report.hits == {}
    assert report.status == "INCOMPLETE"
    assert "partial=1" in report.lines()[0]


def test_a_file_exactly_at_max_bytes_is_not_partial(tmp_path, fakes):
    (tmp_path / "f.bin").write_bytes(b"x" * 1000)
    report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t", max_bytes=1000)
    assert report.partial == 0 and report.status == "PASS"


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 cannot deny a root test run")
def test_d15_c_an_unreadable_file_is_incomplete(tmp_path, fakes):
    locked = tmp_path / "locked.txt"
    locked.write_text(fakes.app_key)
    os.chmod(locked, 0)
    try:
        report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    finally:
        os.chmod(locked, 0o600)
    assert report.unreadable == 1 and report.status == "INCOMPLETE"
    assert report.lines()[0].startswith("INCOMPLETE t unreadable=1 partial=0")


@pytest.mark.skipif(os.geteuid() == 0, reason="chmod 000 cannot deny a root test run")
def test_d15_an_unlistable_directory_is_incomplete(tmp_path, fakes):
    (tmp_path / "d").mkdir()
    os.chmod(tmp_path / "d", 0)
    try:
        report = scan.scan_tree(tmp_path, scan.Spec(fakes.values), label="t")
    finally:
        os.chmod(tmp_path / "d", 0o700)
    assert report.unreadable == 1 and report.status == "INCOMPLETE"


def test_d15_a_missing_required_tree_is_incomplete_an_optional_one_is_absent(tmp_path, fakes):
    spec = scan.Spec(fakes.values)
    required = scan.scan_tree(tmp_path / "nope", spec, label="r")
    assert required.status == "INCOMPLETE" and required.lines()[0].startswith("INCOMPLETE r missing")
    optional = scan.scan_tree(tmp_path / "nope", spec, label="o", required=False)
    assert optional.status == "PASS" and optional.lines() == ["absent o"]


# ------------------------------------------------- git history


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_git_history_finds_a_fake_after_deletion_and_counts_commits(tmp_path, fakes):
    _git(tmp_path, "init", "-q")
    (tmp_path / "README").write_text("clean\n")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-qm", "one")
    (tmp_path / "notes.txt").write_text(f"token {fakes.secret_key}\n")
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-qm", "add")
    _git(tmp_path, "rm", "-q", "notes.txt")
    _git(tmp_path, "commit", "-qm", "remove")
    assert fakes.secret_key not in (tmp_path / "README").read_text()  # gone from the tree
    report = scan.scan_git_history(tmp_path, scan.Spec(fakes.values))
    assert report.status == "FAIL"
    assert set(report.hits) == {"history:secret_key"}
    assert report.commits == 3
    assert report.lines()[0].startswith("FAIL git-history hits=1 commits=3")
    assert fakes.secret_key not in "\n".join(report.lines())


def test_git_history_clean_repo_passes(tmp_path, fakes):
    _git(tmp_path, "init", "-q")
    (tmp_path / "README").write_text("clean\n")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-qm", "one")
    report = scan.scan_git_history(tmp_path, scan.Spec(fakes.values))
    assert (report.status, report.commits) == ("PASS", 1)


def test_git_history_outside_a_repo_is_incomplete(tmp_path, fakes):
    report = scan.scan_git_history(tmp_path, scan.Spec(fakes.values))
    assert report.status == "INCOMPLETE" and report.failed == "git"


def test_d15_h_a_failing_git_runner_is_incomplete(tmp_path, fakes):
    def boom(args, cwd):
        raise scan.CommandFailed

    report = scan.scan_git_history(tmp_path, scan.Spec(fakes.values), run_git=boom)
    assert report.status == "INCOMPLETE"

    def dies_midway(args, cwd):
        yield b"commit " + b"a" * 40 + b"\nclean\n"
        raise scan.CommandFailed

    assert scan.scan_git_history(tmp_path, scan.Spec(fakes.values), run_git=dies_midway).status == "INCOMPLETE"


def test_commit_headers_split_across_chunks_are_counted_once(fakes):
    header = b"commit " + b"b" * 40 + b"\n"
    stream = (header + b"x" * 5000 + b"\n") * 10
    spec = scan.Spec(fakes.values)
    for size in (1, 7, 100, 4999, len(stream)):
        chunks = [stream[i : i + size] for i in range(0, len(stream), size)]
        assert scan.scan_chunks(chunks, spec, count_commits=True).commits == 10, size


# ------------------------------------------------- scan_files and classes


def test_scan_files_over_shell_history_and_gcloud_logs(tmp_path, fakes):
    zsh = tmp_path / ".zsh_history"
    zsh.write_text(f": 1700000000:0;curl -u user:{quote(fakes.secret_key, safe='')} https://x.invalid\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "a.log").write_text(f"ok\n{fakes.app_key}\n")
    (logs / "b.log").write_text("ok\n")
    spec = scan.Spec(fakes.values)
    one = scan.scan_files([zsh, tmp_path / ".bash_history"], spec, label="shell", display=lambda p: "~/" + p.name)
    assert set(one.hits) == {"~/.zsh_history"} and one.absent == 1
    two = scan.scan_tree(logs, spec, label="gcloud-logs", required=False)
    assert set(two.hits) == {"a.log"}


def test_d17_d_a_relay_token_by_value_in_a_history_file(tmp_path, fakes):
    history = tmp_path / ".zsh_history"
    history.write_text(f"echo {fakes.relay_token}\n")
    report = scan.scan_files([history], scan.Spec(fakes.values, shapes=True), label="shell")
    assert report.hits == {str(history): {scan.RELAY_TOKEN}}


def test_d17_g_the_token_file_is_excluded_and_a_copy_is_a_hit(tmp_path, fakes):
    config = tmp_path / ".config" / "growin"
    config.mkdir(parents=True)
    token_file = config / "relay-session.json"
    body = json.dumps({"v": 1, "token": fakes.relay_token, "expires_at_ist": "2099-01-01T00:00:00+05:30"})
    token_file.write_text(body)
    tmp_sibling = config / ".relay-session.json.4242.deadbeef.tmp"
    tmp_sibling.write_text(body)
    spec = scan.Spec(fakes.values, shapes=True)
    excl = scan.Exclusions(token_path=token_file)
    assert scan.scan_tree(config, spec, label="c", exclusions=excl).hits == {}
    (config / "copy.json").write_text(body)
    (config / ".relay-session.json.notes").write_text(body)
    assert set(scan.scan_tree(config, spec, label="c", exclusions=excl).hits) == {"copy.json", ".relay-session.json.notes"}
    # the exclusion is for the relay token only: the AppKey in the token file is still found
    token_file.write_text(body + fakes.app_key)
    assert set(scan.scan_tree(config, spec, label="c", exclusions=excl).hits) >= {"relay-session.json"}


def test_user_id_exclusions_apply_to_the_user_id_class_only(tmp_path, fakes):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "x.json").write_text(json.dumps({"id": fakes.user_id}))
    (tmp_path / "docs" / "icici-docs" / "_local").mkdir(parents=True)
    (tmp_path / "docs" / "icici-docs" / "_local" / "account.md").write_text(fakes.user_id)
    (tmp_path / "elsewhere.txt").write_text(fakes.user_id)
    excl = scan.Exclusions(user_id_dirs=(tmp_path / "private", tmp_path / "docs" / "icici-docs" / "_local"))
    spec = scan.Spec(fakes.values)
    assert set(scan.scan_tree(tmp_path, spec, label="r", exclusions=excl).hits) == {"elsewhere.txt"}
    (tmp_path / "private" / "k.json").write_text(fakes.app_key)
    assert "private/k.json" in scan.scan_tree(tmp_path, spec, label="r", exclusions=excl).hits


# ------------------------------------------------- D11 journal shapes, D17 (f)


def _journal_cases(fakes: Fakes) -> dict[str, tuple[bytes, str]]:
    hexsum = secrets.token_hex(32)
    return {
        "session token": (f"call ok token={fakes.breeze_token} done".encode(), scan.SESSION_TOKEN),
        "x-sessiontoken": (f"X-SessionToken: {fakes.breeze_token}".encode(), scan.SDK_HEADER),
        "x-sessiontoken lower": (f"x-sessiontoken:{fakes.breeze_token}".encode(), scan.SDK_HEADER),
        "x-checksum": (f"X-Checksum: token {hexsum}".encode(), scan.SDK_HEADER),
        "bearer": (f"Authorization: Bearer {fakes.relay_token}".encode(), scan.RELAY_TOKEN),
        "bearer lower": (f"authorization: bearer {fakes.relay_token}".encode(), scan.RELAY_TOKEN),
        "bearer json": (f'"Authorization": "Bearer {fakes.relay_token}"'.encode(), scan.RELAY_TOKEN),
        "relay_token json": (f'{{"relay_token": "{fakes.relay_token}"}}'.encode(), scan.RELAY_TOKEN),
        "apisession": (f"GET /?apisession={fakes.session_code} 200".encode(), scan.SESSION_CODE),
        "API_Session": (f"API_Session={fakes.session_code}".encode(), scan.SESSION_CODE),
        "apisession json": (f'{{"apisession": "{fakes.session_code}"}}'.encode(), scan.SESSION_CODE),
    }


def test_d11_scan_text_flags_every_shape_with_no_raw_secret_known(fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)  # only the user id: no token or code value
    for name, (text, cls) in _journal_cases(fakes).items():
        assert cls in scan.scan_text(text, spec), name


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer short",
        "Authorization: Bearer " + "a" * 44,  # 44: not exactly 43
        "Bearer " + "a" * 43,  # no header name
        "X-SessionToken: abc",
        "apisession=123",
        "apisession_code = something_long_here",
        "the quick brown fox " * 50,
    ],
)
def test_d11_innocent_text_is_not_flagged(text, fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    assert scan.scan_text(text, spec) == set()


def test_session_token_needs_the_right_user_id_and_digits(fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    other = base64.b64encode(f"OTHER{secrets.token_hex(3)}:{_digits(8)}".encode()).decode()
    letters = base64.b64encode(f"{fakes.user_id}:notdigits".encode()).decode()
    assert scan.scan_text(f"x {other} y", spec) == set()
    assert scan.scan_text(f"x {letters} y", spec) == set()
    assert scan.SESSION_TOKEN in scan.scan_text(f"x {fakes.breeze_token} y", spec)


def test_d17_f_a_session_code_in_a_gcloud_log_is_found_by_shape(tmp_path, fakes):
    logs = tmp_path / ".config" / "gcloud" / "logs"
    logs.mkdir(parents=True)
    (logs / "run.log").write_text(f"GET /?apisession={fakes.session_code}\n")
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    assert set(scan.scan_tree(logs, spec, label="gcloud-logs").hits) == {"run.log"}


# ------------------------------------------------- shapes: widths, overlap, chunk edges (D21)

SAMPLES = {
    "bearer": lambda f: f'Authorization": "Bearer  {f.relay_token}'.replace('Authorization"', "Authorization"),
    "relay_token_json": lambda f: f'"relay_token": "{f.relay_token}',
    "api_session": lambda f: f'"apisession": "{f.session_code}',
    "sdk_header": lambda f: f"X-Checksum: token {secrets.token_hex(32)}",
}


def _unbounded(node) -> bool:
    """True if any repeat in a parsed pattern has no upper bound (`*`, `+`, `{n,}`)."""
    for op, av in node:
        if op in (re_constants.MAX_REPEAT, re_constants.MIN_REPEAT, re_constants.POSSESSIVE_REPEAT):
            if av[1] == re_constants.MAXREPEAT:
                return True
            if _unbounded(av[2]):
                return True
            continue
        parts = av if isinstance(av, (tuple, list)) else (av,)
        for part in parts:
            if isinstance(part, re_parser.SubPattern) and _unbounded(part):
                return True
            if isinstance(part, (tuple, list)):
                for sub in part:
                    if isinstance(sub, re_parser.SubPattern) and _unbounded(sub):
                        return True
    return False


def test_d21_p28_the_unbounded_check_catches_a_star_and_a_plus():
    for bad in (rb"x-\S+", rb"a[b]*c", rb"(?:ab)+", rb"a{3,}"):
        assert _unbounded(re_parser.parse(bad)), bad
    for good in (rb"a{0,8}", rb"[A-Za-z0-9+/=]{8,64}", rb"(?:ab){1,3}"):
        assert not _unbounded(re_parser.parse(good)), good


def test_d21_every_shape_is_bounded_and_declares_its_width():
    for cls, name, pattern, declared in scan.SHAPES:
        width = re_parser.parse(pattern, re.IGNORECASE).getwidth()[1]
        assert width < re_constants.MAXREPEAT and width < 1000, name
        assert width == declared, name
        assert not _unbounded(re_parser.parse(pattern, re.IGNORECASE)), f"{name}: unbounded quantifier"
    assert not _unbounded(re_parser.parse(scan.TOKEN_RUN))
    token_width = re_parser.parse(scan.TOKEN_RUN).getwidth()[1]
    assert token_width == scan.TOKEN_RUN_MAX_MATCH < 1000


def test_d21_overlap_covers_the_widest_shape_and_value_form_plus_one(fakes):
    spec = scan.Spec(fakes.values, shapes=True)
    widest = max(m for *_, m in scan.SHAPES)
    assert spec.overlap >= max(widest, scan.TOKEN_RUN_MAX_MATCH) + 1
    assert spec.overlap >= max(len(f) for forms in spec.forms.values() for f in forms) + 1
    short = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)  # short value forms: shapes decide the overlap
    assert short.overlap == max(widest, scan.TOKEN_RUN_MAX_MATCH) + 1


def _edge_file(tmp_path: Path, name: str, match: bytes, start: int, tail: bytes = b" ") -> Path:
    path = tmp_path / name
    size = scan.CHUNK + 4096
    buffer = bytearray(b"." * size)
    buffer[start : start + len(match)] = match
    buffer[start + len(match) : start + len(match) + len(tail)] = tail
    path.write_bytes(bytes(buffer))
    return path


def _offsets(length: int) -> dict[str, int]:
    """Start positions that put the chunk edge in the middle, one character before the end, and
    exactly at the end (so the lookahead character is the first byte of the next chunk)."""
    return {
        "middle": scan.CHUNK - length // 2,
        "one-before-end": scan.CHUNK - (length - 1),
        "at-end": scan.CHUNK - length,
    }


def test_d21_every_shape_is_found_across_a_chunk_edge(tmp_path, fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)  # short forms: only the shapes can reach
    samples = {name: make(fakes).encode() for name, make in SAMPLES.items()}
    samples["session_token"] = fakes.breeze_token.encode()
    expected = {
        "bearer": scan.RELAY_TOKEN,
        "relay_token_json": scan.RELAY_TOKEN,
        "api_session": scan.SESSION_CODE,
        "sdk_header": scan.SDK_HEADER,
        "session_token": scan.SESSION_TOKEN,
    }
    for name, match in samples.items():
        assert expected[name] in scan.scan_text(b" " + match + b" ", spec), f"{name}: sample does not match at all"
        for where, start in _offsets(len(match)).items():
            path = _edge_file(tmp_path, f"{name}-{where}.bin", match, start)
            report = scan.scan_files([path], spec, label="edge")
            assert expected[name] in report.hits.get(str(path), set()), f"{name} at {where}"


def test_d21_the_longest_value_form_is_found_across_a_chunk_edge(tmp_path):
    value = 'q/+"&' * 12  # the escaped and encoded forms differ in length; take the longest
    spec = scan.Spec({scan.APP_KEY: value})
    longest = max(spec.forms[scan.APP_KEY], key=len)
    assert len(longest) > len(value.encode())
    for where, start in _offsets(len(longest)).items():
        path = _edge_file(tmp_path, f"value-{where}.bin", longest, start, tail=b"")
        assert scan.scan_files([path], spec, label="edge").hits, where


def test_d21_p27_an_overlap_sized_from_values_alone_would_miss_a_shape(tmp_path, fakes):
    """Negative control: with the overlap cut to the value forms, the bearer shape split mid-way is missed."""

    class ValueOnly(scan.Spec):
        @property
        def overlap(self) -> int:
            return self.longest_form + 1

    spec = ValueOnly({scan.USER_ID: fakes.user_id}, shapes=True)
    match = SAMPLES["bearer"](fakes).encode()
    path = _edge_file(tmp_path, "bearer.bin", match, scan.CHUNK - len(match) // 2)
    assert scan.scan_files([path], spec, label="edge").hits == {}
    right = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    assert scan.scan_files([path], right, label="edge").hits


# ------------------------------------------------- mac and vm mode


def _mac_env(tmp_path: Path, fakes: Fakes, *, token: bool = True, code: bool = True):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("clean\n")
    home = tmp_path / "home"
    config = home / ".config" / "growin"
    config.mkdir(parents=True)
    token_path = config / "relay-session.json"
    if token:
        token_path.write_text(
            json.dumps({"v": 1, "token": fakes.relay_token, "expires_at_ist": "2099-01-01T00:00:00+05:30"})
        )
    prompts = fakes.prompts if code else (lambda name: "" if name == scan.SESSION_CODE else fakes.prompts(name))

    def clean_git(args, cwd):
        return [b"commit " + b"c" * 40 + b"\nAuthor: x\n\n    init\n"]

    return dict(prompts=prompts, run_git=clean_git, token_path=token_path, repo_root=repo, home=home)


def test_mac_mode_all_clean_prints_pass_lines_and_returns_zero(tmp_path, fakes, capsys):
    code, out = run_scan(capsys, ["mac"], **_mac_env(tmp_path, fakes))
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("PASS working-tree files=")
    assert any(ln.startswith("PASS git-history") and "commits=1" in ln for ln in lines)
    assert "absent shell-history" in lines
    assert lines[-1] == "RESULT PASS"


def test_mac_mode_a_hit_in_the_tree_fails_and_names_the_path_only(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["repo_root"] / "notes.md").write_text(f"SENTINEL-LINE-9 {fakes.app_key} trailing\n")
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1
    assert "FAIL working-tree hits=1" in out and "  hit notes.md app_key" in out
    assert out.splitlines()[-1] == "RESULT FAIL"
    for leaked in (fakes.app_key, "SENTINEL-LINE-9", *scan.expand_forms(fakes.app_key)):
        assert leaked not in out


def test_mac_mode_a_hit_in_git_history_prints_hits_and_commits(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    env["run_git"] = lambda args, cwd: [
        b"commit " + b"d" * 40 + b"\n+" + fakes.secret_key.encode() + b"\ncommit " + b"e" * 40 + b"\n"
    ]
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1
    assert "FAIL git-history hits=1 commits=2" in out
    assert fakes.secret_key not in out


def test_mac_mode_incomplete_exits_two_and_fail_beats_incomplete(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["repo_root"] / "big.bin").write_bytes(b"\0" * (2 * MiB))
    code, out = run_scan(capsys, ["mac"], **env, max_bytes=1 * MiB)
    assert code == 2
    assert "INCOMPLETE working-tree" in out and "partial=1" in out
    assert out.splitlines()[-1] == "RESULT INCOMPLETE"
    (env["repo_root"] / "hit.txt").write_text(fakes.secret_key)
    code, out = run_scan(capsys, ["mac"], **env, max_bytes=1 * MiB)
    assert code == 1 and out.splitlines()[-1] == "RESULT FAIL"


def test_d15_a_failing_git_runner_makes_the_mac_run_incomplete(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)

    def boom(args, cwd):
        raise scan.CommandFailed

    env["run_git"] = boom
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 2 and "INCOMPLETE git-history failed=git" in out


def test_d23_a_missing_token_file_makes_the_repo_targets_incomplete(tmp_path, fakes, capsys):
    code, out = run_scan(capsys, ["mac"], **_mac_env(tmp_path, fakes, token=False))
    assert code == 2
    assert "INCOMPLETE working-tree" in out and "uncovered=relay_token" in out
    assert "INCOMPLETE git-history" in out and out.splitlines()[-1] == "RESULT INCOMPLETE"


def test_d23_a_token_file_and_no_session_code_passes_with_the_uncovered_line(tmp_path, fakes, capsys):
    code, out = run_scan(capsys, ["mac"], **_mac_env(tmp_path, fakes, code=False))
    assert code == 0
    assert "uncovered: session_code (no value entered)" in out.splitlines()
    assert out.splitlines()[-1] == "RESULT PASS"
    (tmp_path / "again").mkdir()
    code, out = run_scan(capsys, ["mac"], **_mac_env(tmp_path / "again", fakes, code=True))
    assert code == 0 and "uncovered: session_code" not in out


def test_d23_an_entered_session_code_is_searched_by_value_in_the_repo(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["repo_root"] / "oops.txt").write_text(f"code {fakes.session_code} here")
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1 and "  hit oops.txt session_code" in out


def test_mac_mode_shell_history_hits_use_the_home_relative_name(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["home"] / ".zsh_history").write_text(f"echo {fakes.relay_token}\n")
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1 and "FAIL shell-history hits=1" in out and "  hit ~/.zsh_history relay_token" in out


def test_d20_runtime_shapes_fail_with_no_value_supplied_on_the_mac(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["home"] / ".bash_history").write_text(f"curl -H 'Authorization: Bearer {secrets.token_urlsafe(32)}' x\n")
    logs = env["home"] / ".config" / "gcloud" / "logs"
    logs.mkdir(parents=True)
    (logs / "a.log").write_text(f"X-SessionToken: {fakes.breeze_token}\n")
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1
    assert "  hit ~/.bash_history relay_token" in out and "  hit a.log sdk_header" in out


def test_mac_mode_refuses_without_a_terminal_unless_prompts_are_injected(capsys):
    code, out = run_scan(capsys, ["mac"])
    assert code == 3 and out.startswith("REFUSED")


# ------------------------------------------------- vm mode


def _vm_env(tmp_path: Path, fakes: Fakes, *, journal: bytes = b"clean journal\n"):
    fs = tmp_path / "fs"
    for rel in ("etc/growin-gateway", "var/lib/private/growin-gateway", "opt/growin-gateway", "tmp", "var/log"):
        (fs / rel).mkdir(parents=True)
    (fs / "etc/growin-gateway/relay.json").write_text(
        json.dumps(
            {
                "expected_userid": fakes.user_id,
                "secret_names": {"app_key": "breeze-api-key", "secret_key": "breeze-api-secret"},
            }
        )
    )

    def fetch(names):
        assert set(names) == {"app_key", "secret_key"}
        return {scan.APP_KEY: fakes.app_key, scan.SECRET_KEY: fakes.secret_key}

    return dict(
        fs_root=fs,
        fetch_secrets=fetch,
        read_journal=lambda unit: [journal],
        geteuid=lambda: 0,
    )


def test_vm_mode_clean_run_passes(tmp_path, fakes, capsys):
    code, out = run_scan(capsys, ["vm"], **_vm_env(tmp_path, fakes))
    assert code == 0
    lines = out.splitlines()
    assert "PASS journal files=1" in lines and "PASS unit-journal files=1" in lines
    assert any(ln.startswith("PASS etc-growin-gateway") for ln in lines)
    assert "absent run" in lines and lines[-1] == "RESULT PASS"


def test_vm_mode_the_user_id_in_relay_json_is_not_a_hit_but_the_app_key_would_be(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    code, _ = run_scan(capsys, ["vm"], **env)
    assert code == 0  # relay.json holds the user id by design
    (env["fs_root"] / "etc/growin-gateway/extra.txt").write_text(fakes.app_key)
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "  hit extra.txt app_key" in out


def test_d17_e_a_bearer_header_in_a_tmp_fixture_is_found_by_shape(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    (env["fs_root"] / "tmp" / "curl.out").write_text(f"> Authorization: Bearer {fakes.relay_token}\n")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "FAIL tmp hits=1" in out and "  hit curl.out relay_token" in out
    assert fakes.relay_token not in out


def test_d20_runtime_shapes_in_journal_var_log_and_history_each_fail(tmp_path, fakes, capsys):
    journal = f"X-SessionToken: {fakes.breeze_token}\n".encode()
    code, out = run_scan(capsys, ["vm"], **_vm_env(tmp_path / "a", fakes, journal=journal))
    assert code == 1 and "  hit journal:sdk_header sdk_header" in out
    env = _vm_env(tmp_path / "b", fakes)
    (env["fs_root"] / "var/log/app.log").write_text(f"X-Checksum: token {secrets.token_hex(32)}\n")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "  hit app.log sdk_header" in out
    env = _vm_env(tmp_path / "c", fakes)
    (env["fs_root"] / "root").mkdir()
    (env["fs_root"] / "root/.bash_history").write_text(f"curl -H 'Authorization: Bearer {secrets.token_urlsafe(32)}'\n")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "  hit .bash_history relay_token" in out


def test_vm_mode_a_value_in_the_journal_is_found_without_printing_it(tmp_path, fakes, capsys):
    code, out = run_scan(
        capsys, ["vm"], **_vm_env(tmp_path, fakes, journal=f"SENTINEL {fakes.secret_key} END\n".encode())
    )
    assert code == 1 and "FAIL journal hits=1" in out and "  hit journal:secret_key secret_key" in out
    assert fakes.secret_key not in out and "SENTINEL" not in out


def test_d15_h_a_failing_journal_reader_is_incomplete(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)

    def boom(unit):
        raise scan.CommandFailed

    env["read_journal"] = boom
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 2 and "INCOMPLETE journal failed=journal" in out


def test_vm_mode_a_missing_required_directory_is_incomplete(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    os.rmdir(env["fs_root"] / "var/lib/private/growin-gateway")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 2 and "INCOMPLETE var-lib-private-growin-gateway missing" in out


def test_vm_mode_failing_secret_fetch_is_incomplete(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)

    def boom(names):
        raise OSError("down")

    env["fetch_secrets"] = boom
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 2 and "INCOMPLETE secret-manager failed=fetch" in out and "uncovered=app_key,secret_key" in out


def test_vm_mode_a_partial_read_is_incomplete(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    (env["fs_root"] / "tmp" / "big.bin").write_bytes(b"\0" * (2 * MiB))
    code, out = run_scan(capsys, ["vm"], **env, max_bytes=1 * MiB)
    assert code == 2 and "INCOMPLETE tmp" in out


def test_vm_mode_refuses_unless_root(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    env["geteuid"] = lambda: 1000
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 3 and out.startswith("REFUSED")


def test_usage_error(capsys):
    assert run_scan(capsys, ["bogus"])[0] == 3
    assert run_scan(capsys, [])[0] == 3


# ------------------------------------------------- output never carries a secret


ALLOWED_LINE = re.compile(
    r"^(PASS|FAIL|INCOMPLETE) [a-z0-9-]+ |^absent [a-z0-9-]+$|^  hit |^uncovered: |^RESULT (PASS|FAIL|INCOMPLETE)$"
)


def test_every_output_line_is_a_fixed_shape_with_no_secret_text(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    (env["repo_root"] / "a.txt").write_text(f"LINE-A {fakes.app_key}\n")
    (env["repo_root"] / "b.txt").write_text(f"LINE-B {fakes.user_id}\n")
    (env["home"] / ".zsh_history").write_text(f"LINE-C {fakes.relay_token}\n")
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1
    for line in out.splitlines():
        assert ALLOWED_LINE.search(line), line
    forms = [f for v in fakes.values.values() for f in scan.expand_forms(v)] + [fakes.breeze_token]
    for form in forms:
        assert form not in out
    for word in ("LINE-A", "LINE-B", "LINE-C"):
        assert word not in out


# ------------------------------------------------- D19: the real tracked tree


def test_d19_value_only_pass_over_the_real_tree_has_zero_hits_and_nothing_incomplete(fakes):
    spec = scan.Spec(fakes.values, shapes=False)  # repo targets are value-only
    excl = scan.Exclusions(user_id_dirs=(ROOT / "private", ROOT / "docs" / "icici-docs" / "_local"))
    report = scan.scan_tree(ROOT, spec, label="working-tree", exclusions=excl)
    assert report.hits == {}, sorted(report.hits)
    assert (report.unreadable, report.partial, report.missing) == (0, 0, False)
    assert report.status == "PASS"
    assert report.files > 100


def test_d19_p25_repo_targets_are_value_only_runtime_targets_take_shapes(tmp_path, fakes, capsys):
    """A tracked test fixture may carry a header shape by design (a fake `Authorization: Bearer <43>`).
    The repo targets must not flag it; the same text in a runtime target must."""
    fixture_text = (
        f"X-SessionToken: {fakes.breeze_token}\n"
        f"Authorization: Bearer {secrets.token_urlsafe(32)}\n"
        f"GET /?apisession={_digits(8)}\n"
    )
    (tmp_path / "a").mkdir()
    env = _mac_env(tmp_path / "a", fakes)
    (env["repo_root"] / "tests").mkdir()
    (env["repo_root"] / "tests" / "test_fixture.py").write_text(fixture_text)
    env["run_git"] = lambda args, cwd: [b"commit " + b"c" * 40 + b"\n+" + fixture_text.encode()]
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 0 and out.splitlines()[-1] == "RESULT PASS", out
    (tmp_path / "b").mkdir()
    env = _mac_env(tmp_path / "b", fakes)
    (env["home"] / ".zsh_history").write_text(fixture_text)
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1 and "FAIL shell-history" in out


def test_the_scanner_and_the_relay_sources_do_not_match_their_own_runtime_shapes(fakes):
    spec = scan.Spec(fakes.values, shapes=True)
    own = scan.scan_files([SCRIPT], spec, label="self")
    assert own.hits == {}, own.hits
    relay = scan.scan_tree(ROOT / "gateway" / "vm" / "gateway_vm", spec, label="relay-src")
    assert relay.hits == {}, relay.hits


# ------------------------------------------------- the script itself


def test_the_script_is_standalone_stdlib_only():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    assert "gateway_vm" not in names


def test_the_script_runs_as_a_program_and_refuses_without_a_terminal():
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "mac"], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert proc.returncode == 3 and proc.stdout.startswith("REFUSED")


# ------------------------------------------------- round 1 fixes


def _session_code_cases() -> dict[str, tuple[str, str]]:
    """Forms the relay's own contract allows (^[A-Za-z0-9]{6,64}$), in the shapes a log can hold."""
    mixed = "AB" + _digits(2) + "cd" + _digits(2)
    tail = _digits(8) + "abc"
    plain = _digits(8)
    return {
        "mixed case": (f"apisession={mixed}", mixed),
        "digits then letters": (f"apisession={tail}", tail),
        "single quoted": (f"apisession='{plain}'", plain),
        "python dict repr": (f"{{'apisession': '{plain}'}}", plain),
        "url encoded equals": (f"apisession%3D{plain}", plain),
        "url encoded equals lower": (f"GET /x?apisession%3d{plain} 200", plain),
        "api_session underscore": (f"api_session={mixed}&x=1", mixed),
        "double quoted json": (f'{{"apisession": "{plain}"}}', plain),
    }


def test_d11_api_session_forms_are_found_by_shape_alone():
    spec = scan.Spec({scan.USER_ID: "FK" + secrets.token_hex(4).upper()}, shapes=True)  # no code known
    for name, (text, _) in _session_code_cases().items():
        assert scan.SESSION_CODE in scan.scan_text(text, spec), name


def test_d11_api_session_forms_are_found_by_value_alone():
    for name, (text, code) in _session_code_cases().items():
        spec = scan.Spec({scan.SESSION_CODE: code}, shapes=False)  # repo targets: value only
        assert scan.SESSION_CODE in scan.scan_text(text, spec), name


def test_d11_api_session_forms_are_found_with_both_value_and_shape():
    for name, (text, code) in _session_code_cases().items():
        spec = scan.Spec({scan.SESSION_CODE: code}, shapes=True)
        assert scan.SESSION_CODE in scan.scan_text(text, spec), name


@pytest.mark.parametrize(
    "text",
    [
        "api_session=apisession",  # the relay's own keyword argument: no digit
        "apisession=abcdefgh",
        "apisession=12345",  # under the relay's lower bound of 6
        "api_session=st.api_session",
        "apisession=" + "1" * 65,  # over the upper bound: not a code
        "apisession: Callable[[], str]",
    ],
)
def test_d11_api_session_innocent_text_is_not_flagged(text, fakes):
    assert scan.scan_text(text, scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)) == set()


def test_d11_the_relay_sources_are_clean_of_the_widened_api_session_shape(fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    relay = scan.scan_tree(ROOT / "gateway" / "vm" / "gateway_vm", spec, label="relay-src")
    assert relay.hits == {}, relay.hits


def test_d21_api_session_is_found_across_a_chunk_edge_in_every_form(tmp_path, fakes):
    spec = scan.Spec({scan.USER_ID: fakes.user_id}, shapes=True)
    for name, (text, _) in _session_code_cases().items():
        match = (" " + text + " ").encode()
        for where, start in _offsets(len(match)).items():
            path = _edge_file(tmp_path, f"api-{name.replace(' ', '-')}-{where}.bin", match, start)
            report = scan.scan_files([path], spec, label="edge")
            assert scan.SESSION_CODE in report.hits.get(str(path), set()), f"{name} at {where}"


def test_item2_a_path_carrying_a_header_shape_is_redacted(tmp_path, fakes, capsys):
    other = secrets.token_urlsafe(32)  # not a known value: only the runtime shape can recognise it
    name = f"Authorization: Bearer {other}.log"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / name).write_text(fakes.app_key)
    report = scan.scan_tree(logs, scan.Spec(fakes.values, shapes=False), label="t")  # a repo-style spec too
    text = "\n".join(report.lines())
    assert other not in text and "<redacted-path-1>" in text and report.status == "FAIL"


def test_item2_the_mac_run_never_prints_a_token_carried_by_a_filename(tmp_path, fakes, capsys):
    env = _mac_env(tmp_path, fakes)
    other = secrets.token_urlsafe(32)
    logs = env["home"] / ".config" / "gcloud" / "logs"
    logs.mkdir(parents=True)
    (logs / f"X-SessionToken: {fakes.breeze_token}.log").write_text(fakes.app_key)
    (logs / f"Authorization: Bearer {other}.log").write_text(fakes.app_key)
    (logs / f"apisession={fakes.session_code}.log").write_text(fakes.app_key)
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 1 and "FAIL gcloud-logs hits=3" in out
    for leaked in (other, fakes.breeze_token, fakes.session_code):
        assert leaked not in out
    assert out.count("<redacted-path-") == 3


def test_item3_the_vm_scans_etc_and_finds_an_appkey_in_a_unit_drop_in(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    drop_in = env["fs_root"] / "etc/systemd/system/growin-gateway.service.d"
    drop_in.mkdir(parents=True)
    (drop_in / "override.conf").write_text(f"[Service]\nEnvironment=K={fakes.app_key}\n")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "FAIL etc hits=1" in out
    assert "  hit systemd/system/growin-gateway.service.d/override.conf app_key" in out
    assert fakes.app_key not in out


def test_item3_a_token_in_etc_environment_is_found_by_shape(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)
    (env["fs_root"] / "etc" / "environment").write_text(f"AUTH=Authorization: Bearer {secrets.token_urlsafe(32)}\n")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "  hit environment relay_token" in out


def test_item3_etc_keeps_the_user_id_exclusion_for_the_gateway_directory(tmp_path, fakes, capsys):
    env = _vm_env(tmp_path, fakes)  # relay.json holds the user id by design
    (env["fs_root"] / "etc" / "motd").write_text(fakes.user_id)
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 1 and "  hit motd user_id" in out and "hit growin-gateway/relay.json" not in out
    (env["fs_root"] / "etc" / "motd").unlink()
    assert run_scan(capsys, ["vm"], **env)[0] == 0


def test_item3_a_missing_etc_is_incomplete(tmp_path, fakes, capsys):
    import shutil

    env = _vm_env(tmp_path, fakes)
    shutil.rmtree(env["fs_root"] / "etc")
    code, out = run_scan(capsys, ["vm"], **env)
    assert code == 2 and "INCOMPLETE etc missing" in out


def test_item4_a_value_under_the_floor_is_never_covered():
    short = "AB" + _digits(5)  # seven characters: the raw form falls under the 8-character floor
    spec = scan.Spec({scan.USER_ID: short}, shapes=True)
    assert spec.uncovered([scan.USER_ID]) == [scan.USER_ID]
    assert scan.expand_forms(short) != []  # its base64 form is still searched, but that is not coverage
    assert scan.Spec({scan.USER_ID: "AB" + _digits(6)}).uncovered([scan.USER_ID]) == []


def test_item4_a_short_user_id_is_incomplete_on_the_mac_and_on_the_vm(tmp_path, fakes, capsys):
    fakes.user_id = "AB" + _digits(5)
    (tmp_path / "m").mkdir()
    env = _mac_env(tmp_path / "m", fakes)
    (env["repo_root"] / "plain.txt").write_text(f"user={fakes.user_id}\n")  # the raw form passes unseen
    code, out = run_scan(capsys, ["mac"], **env)
    assert code == 2 and "uncovered=user_id" in out and out.splitlines()[-1] == "RESULT INCOMPLETE"
    (tmp_path / "v").mkdir()
    code, out = run_scan(capsys, ["vm"], **_vm_env(tmp_path / "v", fakes))
    assert code == 2 and "uncovered=user_id" in out


def test_item4_a_short_session_code_says_why_it_is_uncovered(tmp_path, fakes, capsys):
    fakes.session_code = _digits(7)
    code, out = run_scan(capsys, ["mac"], **_mac_env(tmp_path, fakes))
    assert "uncovered: session_code (value under 8 characters)" in out.splitlines()
    assert code == 2


def _binary_repo(repo: Path, payload: bytes) -> None:
    _git(repo, "init", "-q")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02" + payload + b"\x00")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "binary")


def test_item7_a_secret_in_a_binary_diff_is_found_in_git_history(tmp_path, fakes):
    _binary_repo(tmp_path, fakes.secret_key.encode())
    report = scan.scan_git_history(tmp_path, scan.Spec(fakes.values))
    assert report.status == "FAIL" and set(report.hits) == {"history:secret_key"}


def test_item7_the_git_command_asks_for_text_diffs(tmp_path, fakes):
    seen = []

    def runner(args, cwd):
        seen.append(list(args))
        return [b"commit " + b"a" * 40 + b"\n"]

    scan.scan_git_history(tmp_path, scan.Spec(fakes.values), run_git=runner)
    assert seen == [["log", "-p", "--all", "--no-color", "--text"]]
