"""Relay-token store on the Mac (Phase 61-09, OD-16, T-61-84).

The file store is checked against a real temp directory: mode, owner, symlink,
parent directory, expiry, version and token-shape rules, plus a write that goes
through an exclusive no-follow temp file. Tokens here are generated fakes.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from test_gateway_relay_client import (
    CODE,
    EXPIRY,
    HEALTH_JSON,
    OTHER_TOKEN,
    TOKEN,
    Canned,
    error_response,
    json_response,
    mock_http,
)

from brokers.breeze_gateway import (
    FileTokenStore,
    MemoryTokenStore,
    RelayClient,
    SessionExpired,
    StoredToken,
)
from brokers.breeze_gateway import token_store as token_store_mod
from brokers.breeze_gateway.config import parse_gateway_config
from test_gateway_tunnel_argv import VALID, Factory, FakeTunnel

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "backend" / "brokers" / "breeze_gateway"
NOW = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)


def store_at(directory: Path, **kwargs) -> FileTokenStore:
    kwargs.setdefault("now", lambda: NOW)
    return FileTokenStore(directory / "relay-session.json", **kwargs)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    directory = tmp_path / "growin"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory


def payload(**over) -> dict:
    base = {"v": 1, "token": TOKEN, "expires_at_ist": EXPIRY.isoformat()}
    base.update(over)
    return base


def plant(vault: Path, data, mode: int = 0o600) -> Path:
    path = vault / "relay-session.json"
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    path.chmod(mode)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ------------------------------------------------------------------ memory


def test_memory_store_round_trips_and_clears():
    store = MemoryTokenStore()
    assert store.load() is None
    store.save(TOKEN, EXPIRY)
    loaded = store.load()
    assert loaded is not None and loaded.token == TOKEN and loaded.expires_at_ist == EXPIRY
    store.clear()
    store.clear()
    assert store.load() is None


def test_stored_token_hides_the_token_in_repr_and_str():
    stored = StoredToken(token=TOKEN, expires_at_ist=EXPIRY)
    assert TOKEN not in repr(stored) and TOKEN not in str(stored)
    assert "token" not in repr(stored)


# --------------------------------------------------------------------- save


def test_save_creates_the_directory_at_0700_and_the_file_at_0600_whatever_the_umask(tmp_path):
    target = tmp_path / "fresh" / "growin"
    old = os.umask(0)
    try:
        FileTokenStore(target / "relay-session.json", now=lambda: NOW).save(TOKEN, EXPIRY)
    finally:
        os.umask(old)
    assert mode_of(target) == 0o700
    assert mode_of(target / "relay-session.json") == 0o600
    assert json.loads((target / "relay-session.json").read_text()) == payload()
    assert [p.name for p in target.iterdir()] == ["relay-session.json"]  # no temp left behind


def test_save_forces_0600_even_under_a_restrictive_umask(vault):
    old = os.umask(0o277)  # open(..., 0o600) alone would leave 0400
    try:
        store_at(vault).save(TOKEN, EXPIRY)
    finally:
        os.umask(old)
    assert mode_of(vault / "relay-session.json") == 0o600
    assert store_at(vault).load() is not None


def test_save_goes_through_an_exclusive_no_follow_temp_file_that_is_synced_and_renamed(vault, monkeypatch):
    opened, synced, replaced = [], [], []
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def spy_open(path, flags, mode=0o777, **kw):
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real_replace(a, b))[1])
    store_at(vault).save(TOKEN, EXPIRY)

    writes = [o for o in opened if o[1] & os.O_WRONLY]
    assert len(writes) == 1
    path, flags, mode = writes[0]
    need = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    assert flags & need == need and mode == 0o600
    assert path.endswith(".tmp") and Path(path).parent == vault
    assert synced, "the file is fsynced before the rename"
    assert replaced == [(path, str(vault / "relay-session.json"))]


def test_save_never_writes_through_a_planted_temp_symlink(vault, monkeypatch):
    victim = vault.parent / "victim.txt"
    victim.write_text("keep")
    monkeypatch.setattr(token_store_mod.secrets, "token_hex", lambda n: "deadbeef")
    (vault / f".relay-session.json.{os.getpid()}.deadbeef.tmp").symlink_to(victim)
    with pytest.raises(OSError):
        store_at(vault).save(TOKEN, EXPIRY)
    assert victim.read_text() == "keep"
    assert not (vault / "relay-session.json").exists()


def test_save_replaces_a_symlink_at_the_target_without_touching_its_destination(vault):
    victim = vault.parent / "victim.txt"
    victim.write_text("keep")
    (vault / "relay-session.json").symlink_to(victim)
    store_at(vault).save(TOKEN, EXPIRY)
    assert victim.read_text() == "keep"
    assert not (vault / "relay-session.json").is_symlink()
    assert store_at(vault).load() is not None


def test_save_replaces_the_previous_token(vault):
    store = store_at(vault)
    store.save(TOKEN, EXPIRY)
    store.save(OTHER_TOKEN, EXPIRY)
    loaded = store.load()
    assert loaded is not None and loaded.token == OTHER_TOKEN
    assert len(list(vault.iterdir())) == 1


def test_save_refuses_a_loose_parent_directory_and_writes_nothing(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    with pytest.raises(PermissionError):
        FileTokenStore(loose / "relay-session.json", now=lambda: NOW).save(TOKEN, EXPIRY)
    assert list(loose.iterdir()) == []
    assert mode_of(loose) == 0o755  # an existing directory is never changed behind the user's back


@pytest.mark.parametrize("token", ["", "short", "x" * 42, "x" * 44, "x" * 42 + "!", 7])
def test_save_refuses_a_malformed_token(vault, token):
    with pytest.raises(ValueError):
        store_at(vault).save(token, EXPIRY)
    assert list(vault.iterdir()) == []


def test_save_refuses_a_naive_expiry(vault):
    with pytest.raises(ValueError):
        store_at(vault).save(TOKEN, datetime(2026, 10, 22))
    assert list(vault.iterdir()) == []


def test_a_failed_rename_leaves_no_temp_file(vault, monkeypatch):
    def boom(a, b):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store_at(vault).save(TOKEN, EXPIRY)
    assert list(vault.iterdir()) == []


# --------------------------------------------------------------------- load


def test_load_returns_a_valid_saved_token(vault):
    store_at(vault).save(TOKEN, EXPIRY)
    loaded = store_at(vault).load()
    assert isinstance(loaded, StoredToken)
    assert loaded.token == TOKEN and loaded.expires_at_ist == EXPIRY


def test_load_of_a_missing_file_is_none(vault):
    assert store_at(vault).load() is None
    assert FileTokenStore(vault.parent / "nowhere" / "x.json", now=lambda: NOW).load() is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o400, 0o666])
def test_load_refuses_any_mode_but_0600(vault, mode):
    plant(vault, payload(), mode=mode)
    assert store_at(vault).load() is None


def test_load_refuses_a_file_owned_by_someone_else(vault):
    plant(vault, payload())
    assert store_at(vault, uid=os.geteuid() + 1).load() is None
    assert store_at(vault).load() is not None


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777, 0o500])
def test_load_refuses_a_parent_directory_other_than_0700(vault, mode):
    plant(vault, payload())
    vault.chmod(mode)
    try:
        assert store_at(vault).load() is None
    finally:
        vault.chmod(0o700)


def test_load_refuses_a_parent_owned_by_someone_else(vault):
    plant(vault, payload())
    assert store_at(vault, uid=os.geteuid() + 1).load() is None


def _other_owner(real):
    def wrapped(*args, **kwargs):
        info = real(*args, **kwargs)
        fields = list(info)[:10]
        fields[4] = info.st_uid + 1
        return os.stat_result(fields)

    return wrapped


def test_the_file_owner_is_checked_on_its_own(vault, monkeypatch):
    plant(vault, payload())
    monkeypatch.setattr(os, "fstat", _other_owner(os.fstat))  # only the open file looks foreign
    assert store_at(vault).load() is None


def test_the_parent_owner_is_checked_on_its_own(vault, monkeypatch):
    plant(vault, payload())
    monkeypatch.setattr(os, "lstat", _other_owner(os.lstat))  # only the directory looks foreign
    assert store_at(vault).load() is None


def test_load_refuses_a_file_over_the_size_cap_even_when_it_is_valid_json(vault):
    pad = b" " * token_store_mod.MAX_FILE_BYTES
    plant(vault, json.dumps(payload()).encode() + pad)
    assert store_at(vault).load() is None
    plant(vault, json.dumps(payload()).encode())
    assert store_at(vault).load() is not None


def test_load_refuses_a_symlink_even_to_a_valid_file(vault, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(payload()))
    real.chmod(0o600)
    (vault / "relay-session.json").symlink_to(real)
    assert store_at(vault).load() is None


def test_load_refuses_a_symlinked_parent_directory(vault, tmp_path):
    plant(vault, payload())
    alias = tmp_path / "alias"
    alias.symlink_to(vault)
    assert FileTokenStore(alias / "relay-session.json", now=lambda: NOW).load() is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'"x"',
        json.dumps(payload(v=2)).encode(),
        json.dumps(payload(v=True)).encode(),
        json.dumps(payload(v="1")).encode(),
        json.dumps(payload(token="x" * 42)).encode(),
        json.dumps(payload(token="x" * 44)).encode(),
        json.dumps(payload(token="x" * 42 + "!")).encode(),
        json.dumps(payload(token=7)).encode(),
        json.dumps({"v": 1, "token": TOKEN}).encode(),
        json.dumps(payload(extra=1)).encode(),
        json.dumps(payload(expires_at_ist="soon")).encode(),
        json.dumps(payload(expires_at_ist=5)).encode(),
        json.dumps(payload(expires_at_ist="2026-10-22T00:00:00")).encode(),  # naive
        b"x" * 5000,
    ],
)
def test_load_returns_none_for_any_malformed_file_and_never_raises(vault, data):
    plant(vault, data)
    assert store_at(vault).load() is None


def test_load_refuses_an_expired_token_including_the_exact_expiry(vault):
    plant(vault, payload())
    assert store_at(vault, now=lambda: EXPIRY - timedelta(seconds=1)).load() is not None
    assert store_at(vault, now=lambda: EXPIRY).load() is None
    assert store_at(vault, now=lambda: EXPIRY + timedelta(hours=1)).load() is None


def test_load_never_raises_on_a_directory_or_unreadable_path(vault):
    (vault / "relay-session.json").mkdir()
    assert store_at(vault).load() is None


# -------------------------------------------------------------------- clear


def test_clear_removes_the_file_and_is_idempotent(vault):
    store = store_at(vault)
    store.save(TOKEN, EXPIRY)
    store.clear()
    assert not (vault / "relay-session.json").exists()
    store.clear()


def test_clear_removes_a_symlink_but_not_what_it_points_to(vault, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (vault / "relay-session.json").symlink_to(victim)
    store_at(vault).clear()
    assert victim.read_text() == "keep"
    assert not (vault / "relay-session.json").is_symlink()


# ------------------------------------------------- nothing logs or prints


@pytest.mark.parametrize("name", ["token_store", "client", "tunnel", "config", "errors"])
def test_the_module_neither_logs_nor_prints(name):
    tree = ast.parse((PKG / f"{name}.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "logging" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "logging"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"print", "pprint"}


# --------------------------------------- the token file through the client


def start_open(vault: Path, **store_kwargs):
    supervisor, factory = FakeTunnel(), Factory()
    store = store_at(vault, **store_kwargs)
    relay = RelayClient.open(
        parse_gateway_config(VALID), token_store=store, tunnel=supervisor, http_factory=factory
    )
    return relay, store, factory.handler


def authorizations(handler: Canned) -> list[str | None]:
    return [r.headers.get("authorization") for r in handler.requests]


def test_open_sends_a_valid_saved_token_on_the_first_data_call(vault):
    store_at(vault).save(TOKEN, EXPIRY)
    relay, _, handler = start_open(vault)
    relay.budget()
    assert authorizations(handler) == [None, f"Bearer {TOKEN}"]  # health sends none
    relay.close()


@pytest.mark.parametrize("why", ["expired", "bad_mode", "bad_json", "bad_token"])
def test_open_ignores_and_removes_an_unusable_saved_file(vault, why):
    data = {
        "expired": payload(expires_at_ist=(NOW - timedelta(hours=1)).isoformat()),
        "bad_mode": payload(),
        "bad_json": b"{",
        "bad_token": payload(token="short"),
    }[why]
    path = plant(vault, data, mode=0o644 if why == "bad_mode" else 0o600)
    relay, _, handler = start_open(vault)
    assert not path.exists()
    relay.budget()
    assert authorizations(handler) == [None, None]
    relay.close()


def test_login_saves_the_token_file_and_revoke_removes_it(vault):
    relay, store, _ = start_open(vault)
    attempt = relay.login_arm()
    relay.login_complete(attempt.attempt_id, CODE)
    path = vault / "relay-session.json"
    assert mode_of(path) == 0o600 and mode_of(vault) == 0o700
    assert json.loads(path.read_text())["token"] == attempt.relay_token
    relay.revoke()
    assert not path.exists()
    relay.close()


def test_a_dead_session_response_removes_the_token_file(vault):
    path = plant(vault, payload())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return json_response(HEALTH_JSON)
        return error_response("SESSION_EXPIRED", 503, reason="deadline")

    supervisor, factory = FakeTunnel(), Factory(handler)
    relay = RelayClient.open(
        parse_gateway_config(VALID), token_store=store_at(vault), tunnel=supervisor, http_factory=factory
    )
    with pytest.raises(SessionExpired):
        relay.budget()
    assert not path.exists()
    relay.close()


def test_a_token_file_that_cannot_be_written_does_not_break_the_login(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    store = FileTokenStore(loose / "relay-session.json", now=lambda: NOW)
    handler = Canned()
    relay = RelayClient(http=mock_http(handler), token_store=store)
    attempt = relay.login_arm()
    relay.login_complete(attempt.attempt_id, CODE)
    assert list(loose.iterdir()) == []  # nothing was written into the loose directory
    relay.budget()
    assert handler.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"  # still works in memory
