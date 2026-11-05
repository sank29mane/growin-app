"""GET /v1/orders/build and OrdersClient.current_build (64-05 Task 2b, OI-9).

The VM half runs the real in-process OrderStack with the build route mounted over a tmp package
tree (a stamped copy of the layout the deploy archive makes), so the commit and the file hashes
are known. The Mac half runs a real RelayClient over that VM or over httpx.MockTransport canned
replies. No tunnel, gcloud, real key, account id, IP or non-loopback socket.

Where this file sits against the others: route-table and bearer pins for the route are in
test_gateway_orders_routes.py (ORDER_TABLE) and the K11 scan of the handler is in
test_gateway_orders_key.py. The digest recipe is pinned here once, against all three
implementations (VM, replay, Mac).
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from test_gateway_orders_key import (  # noqa: E402,F401  (fixtures reused by name)
    HEX_A,
    MAC_TOKEN,
    Recorder,
    _budget_exhausted,
    _egress_evidence_aged_out,
    _kill_absent,
    _kill_blocked,
    canned_client,
    mac_store,
    metadata,
    orders_error,
    orders_reply,
    relogin,
    request_events,
    run_cli,
    tree_snapshot,
)
from test_gateway_orders_routes import (  # noqa: E402
    ORDERS_HDR,
    OrderStack,
    assert_orders_envelope,
)

import shadow_india.vm_replay as vm_replay  # noqa: E402
from brokers.breeze_gateway import cli as cli_mod  # noqa: E402
from brokers.breeze_gateway import orders as mac_orders  # noqa: E402
from brokers.breeze_gateway.client import ORDERS_CONTRACT as MAC_ORDERS_CONTRACT  # noqa: E402
from brokers.breeze_gateway.client import RelayClient  # noqa: E402
from brokers.breeze_gateway.errors import (  # noqa: E402
    MethodNotAllowed,
    NotFound,
    OrdersBuildInvalid,
    OrdersBuildRouteUnavailable,
    OrdersBuildUnstamped,
    OrdersUnavailable,
    RelayContractMismatch,
    Unauthorized,
)
from brokers.breeze_gateway.orders import OrdersClient, VmBuild  # noqa: E402
from gateway_vm import app as relay_app  # noqa: E402
from gateway_vm import contract as vm_contract  # noqa: E402
from gateway_vm import orders as vm_orders  # noqa: E402
from gateway_vm.orders import build as vm_build  # noqa: E402

BUILD_PATH = "/v1/orders/build"
COMMIT = "1234567890abcdef1234567890abcdef12345678"
REAL_PACKAGE = ROOT / "gateway" / "vm" / "gateway_vm"
GOLDEN_MAP = {"b.py": "b" * 64, "a.py": "a" * 64, "data/x.json": "c" * 64}
# sha256 of the literal text below, written out by hand so no implementation computes its own answer:
# {"commit":"1234...5678","orders":{"a.py":"aa..","b.py":"bb..","data/x.json":"cc.."}}
GOLDEN_DIGEST = "e5246d855df2cad80a63a193d9851f8f3aeae3062109df044ccaee50a83b45f4"

TREE = {
    "__init__.py": b"# orders\n",
    "build.py": b"x = 1\n",
    "data/holidays.json": b'{"h": []}\n',
    "data/deep/more.txt": b"more\n",
}


def write_tree(package_dir: Path, *, commit: str | None = COMMIT, files: dict[str, bytes] | None = None) -> None:
    """The deployed layout: ``<package_dir>/BUILD_COMMIT`` and ``<package_dir>/orders/**``."""
    package_dir.mkdir(parents=True, exist_ok=True)
    if commit is not None:
        (package_dir / "BUILD_COMMIT").write_bytes(commit.encode("ascii") + b"\n")
    for name, data in (TREE if files is None else files).items():
        path = package_dir / "orders" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def package(tmp_path, monkeypatch):
    """A stamped package tree, and create_app pointed at it instead of the real gateway_vm."""
    package_dir = tmp_path / "deployed" / "gateway_vm"
    write_tree(package_dir)
    real = relay_app.mount_build_route
    monkeypatch.setattr(
        relay_app,
        "mount_build_route",
        lambda app, *, session, bearer, package_dir: real(  # the real gateway_vm directory is replaced
            app, session=session, bearer=bearer, package_dir=tmp_path / "deployed" / "gateway_vm"
        ),
    )
    return package_dir


@pytest.fixture()
def stack(tmp_path, metadata, package):
    built = OrderStack(tmp_path, metadata)
    yield built
    assert built.transport.order_endpoint_non_get() == []
    assert built.transport.non_get() == []
    assert set(metadata.methods) <= {"GET"}


@pytest.fixture()
def unstamped_stack(tmp_path, metadata):
    """No monkeypatch: create_app mounts the real gateway_vm, whose committed stamp is the placeholder."""
    built = OrderStack(tmp_path, metadata)
    yield built


@pytest.fixture()
def token(stack) -> str:
    return stack.login()


def build_read(stack, bearer, **kwargs):
    return stack.get(BUILD_PATH, bearer, **kwargs)


def expected_body(package_dir: Path) -> dict:
    orders = {
        path.relative_to(package_dir / "orders").as_posix(): sha(path.read_bytes())
        for path in sorted((package_dir / "orders").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    commit = (package_dir / "BUILD_COMMIT").read_text(encoding="ascii").strip()
    text = json.dumps({"commit": commit, "orders": orders}, sort_keys=True, separators=(",", ":"))
    return {
        "contract": vm_orders.CONTRACT,
        "commit": commit,
        "orders_sha256": orders,
        "digest": sha(text.encode("ascii")),
    }


# ---------------------------------------------------------------- hash rule parity with the replay


def test_the_vm_tree_hash_equals_the_replays_on_both_layouts(tmp_path):
    # The replay reads <vm_source>/gateway_vm/orders; the route hashes <package_dir>/orders.
    vm_source = tmp_path / "replay_side"
    package_dir = tmp_path / "route_side" / "gateway_vm"
    for root in (vm_source / "gateway_vm", package_dir):
        write_tree(root)
        pycache = root / "orders" / "__pycache__"
        pycache.mkdir()
        (pycache / "x.pyc").write_bytes(b"\x00compiled")
        (root / "orders" / "data" / "__pycache__").mkdir()
        (root / "orders" / "data" / "__pycache__" / "y.pyc").write_bytes(b"\x01")
    identity = vm_build.read_build_identity(package_dir)
    assert isinstance(identity, vm_build.BuildIdentity)
    replay_map = vm_replay.orders_tree_sha256(vm_source)
    assert dict(identity.orders_sha256) == replay_map
    assert set(replay_map) == set(TREE)  # nested data/ files in, every __pycache__ out, posix relative keys
    assert all(not key.startswith("/") and "\\" not in key for key in identity.orders_sha256)


def test_the_vm_digest_equals_the_replays_for_the_same_tree(tmp_path):
    package_dir = tmp_path / "gateway_vm"
    write_tree(package_dir)
    identity = vm_build.read_build_identity(package_dir)
    replay = vm_replay.BuildIdentity(COMMIT, vm_replay.orders_tree_sha256(tmp_path))
    assert identity.digest == replay.digest


# ---------------------------------------------------------------- M5: one digest recipe, three places


def test_the_digest_recipe_has_one_golden_value_in_all_three_implementations():
    assert vm_build.build_digest(COMMIT, GOLDEN_MAP) == GOLDEN_DIGEST
    assert vm_replay.BuildIdentity(COMMIT, GOLDEN_MAP).digest == GOLDEN_DIGEST
    assert mac_orders.build_digest(COMMIT, GOLDEN_MAP) == GOLDEN_DIGEST
    # And the client's own recompute accepts it on the wire (the Mac path, not just the helper).
    body = {
        "contract": MAC_ORDERS_CONTRACT,
        "commit": COMMIT,
        "orders_sha256": GOLDEN_MAP,
        "digest": GOLDEN_DIGEST,
    }
    client, _ = canned_client(orders_reply(body))
    assert client.current_build().digest == GOLDEN_DIGEST


# ---------------------------------------------------------------- the route, stamped


def test_the_route_answers_the_commit_the_file_hashes_and_the_digest(stack, token):
    resp = build_read(stack, token)
    assert resp.status_code == 200, resp.text
    assert_orders_envelope(resp)
    body = resp.json()
    assert set(body) == {"contract", "commit", "orders_sha256", "digest"}
    assert body == expected_body(stack.tmp / "deployed" / "gateway_vm")
    assert body["commit"] == COMMIT
    assert set(body["orders_sha256"]) == set(TREE)
    assert body["digest"] == vm_build.build_digest(COMMIT, body["orders_sha256"])


def test_hashes_are_computed_from_the_files_not_read_from_a_stamp(tmp_path, metadata, monkeypatch):
    package_dir = tmp_path / "deployed" / "gateway_vm"
    write_tree(package_dir)
    # A record made at stamp time, next to the stamp. A route that trusted it would report these.
    stale = {name: sha(data) for name, data in TREE.items()}
    (package_dir / "BUILD_ORDERS_SHA256.json").write_text(json.dumps(stale), encoding="ascii")
    (package_dir / "orders" / "build.py").write_bytes(b"x = 2  # edited after the stamp\n")
    real = relay_app.mount_build_route
    monkeypatch.setattr(
        relay_app,
        "mount_build_route",
        lambda app, *, session, bearer, package_dir: real(app, session=session, bearer=bearer, package_dir=tmp_path / "deployed" / "gateway_vm"),
    )
    stack = OrderStack(tmp_path, metadata)
    answered = build_read(stack, stack.login()).json()
    assert answered["orders_sha256"]["build.py"] == sha(b"x = 2  # edited after the stamp\n")
    assert answered["orders_sha256"]["build.py"] != stale["build.py"]
    assert answered["commit"] == COMMIT


def test_the_identity_is_computed_once_at_mount(stack, token, package):
    first = build_read(stack, token).json()
    (package / "orders" / "build.py").write_bytes(b"x = 3  # edited while the VM runs\n")
    (package / "BUILD_COMMIT").write_bytes(b"f" * 40 + b"\n")
    (package / "orders" / "added_later.py").write_bytes(b"y = 1\n")
    assert build_read(stack, token).json() == first
    assert "added_later.py" not in first["orders_sha256"]


def test_create_app_hands_the_route_the_gateway_vm_package_directory():
    # The mount call in app.py names the directory that holds BUILD_COMMIT and orders/.
    source = (REAL_PACKAGE / "app.py").read_text(encoding="utf-8")
    assert source.count("mount_build_route(") == 1
    assert "package_dir=Path(__file__).parent" in source
    assert (REAL_PACKAGE / "app.py").parent == REAL_PACKAGE
    assert (REAL_PACKAGE / "orders").is_dir() and (REAL_PACKAGE / "BUILD_COMMIT").is_file()


@pytest.mark.parametrize("query", ["x=1", "commit=abc", "a=1&a=2"], ids=["unknown", "commit", "repeated"])
def test_any_query_string_is_a_422(stack, token, query):
    resp = stack.get(f"{BUILD_PATH}?{query}", token)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION"
    assert COMMIT not in resp.text


# ---------------------------------------------------------------- unstamped refuses


def unstamped_wire(resp) -> None:
    assert resp.status_code == 503, resp.text
    assert_orders_envelope(resp)
    error = resp.json()["error"]
    assert (error["code"], error["reason"]) == ("ORDERS_UNAVAILABLE", "build_unstamped")
    assert "commit" not in resp.json()


UNSTAMPED_FILES = {
    "placeholder": b"$Format:%H$\n",
    "empty": b"",
    "blank_line": b"\n",
    "len_39": b"a" * 39 + b"\n",
    "len_41": b"a" * 41 + b"\n",
    "uppercase": b"A" * 40 + b"\n",
    "mixed_case": b"a" * 39 + b"B\n",
    "non_hex": b"a" * 39 + b"g\n",
    "two_lines": b"a" * 40 + b"\n" + b"a" * 40 + b"\n",
    "non_ascii": b"\xc3\xa9" + b"a" * 38 + b"\n",
}


@pytest.mark.parametrize("content", UNSTAMPED_FILES.values(), ids=UNSTAMPED_FILES.keys())
def test_a_missing_or_malformed_stamp_is_503_build_unstamped(stack, package, content):
    (package / "BUILD_COMMIT").write_bytes(content)
    stack.restart()
    unstamped_wire(build_read(stack, stack.login()))


def test_a_missing_stamp_file_is_503_build_unstamped(stack, package):
    (package / "BUILD_COMMIT").unlink()
    stack.restart()
    unstamped_wire(build_read(stack, stack.login()))


def test_the_unstamped_answer_keeps_its_own_code_and_is_logged_as_other(stack, package):
    (package / "BUILD_COMMIT").write_bytes(b"$Format:%H$\n")
    stack.restart()
    token = stack.login()
    resp = build_read(stack, token)
    unstamped_wire(resp)
    # Not rewritten to config_invalid by refusal(), and no new CODE_TABLE row: the log word is `other`.
    assert "build_unstamped" not in vm_orders.CODE_TABLE
    assert resp.json()["error"]["reason"] != "config_invalid"
    event = request_events(stack)[-1]
    assert (event["route"], event["http"], event["reason"]) == (BUILD_PATH, 503, "other")


def test_the_real_gateway_vm_checkout_is_unstamped_until_an_archive_expands_it(unstamped_stack):
    token = unstamped_stack.login()
    unstamped_wire(build_read(unstamped_stack, token))


@pytest.mark.parametrize(
    "files",
    [
        {},
        {f"f{i:02d}.py": b"x" for i in range(65)},
        {"README.md": b"upper case name"},
        {"has space.py": b"x"},
        {"a" * 97: b"x"},
    ],
    ids=["no_files", "65_files", "uppercase_name", "space_in_name", "97_char_name"],
)
def test_a_tree_the_mac_would_refuse_is_refused_by_the_vm_first(stack, package, files):
    for path in sorted((package / "orders").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    for name, data in files.items():
        (package / "orders" / name).write_bytes(data)
    stack.restart()
    unstamped_wire(build_read(stack, stack.login()))


def test_a_sixty_four_file_tree_is_answered(stack, package):
    for path in sorted((package / "orders").rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    for i in range(64):
        (package / "orders" / f"f{i:02d}.py").write_bytes(b"x")
    stack.restart()
    resp = build_read(stack, stack.login())
    assert resp.status_code == 200 and len(resp.json()["orders_sha256"]) == 64


# ---------------------------------------------------------------- committed files


def test_the_committed_stamp_is_the_unexpanded_placeholder():
    # A real 40-hex line here would mean `export-subst` was bypassed by hand: archives would carry a
    # stale commit. The deploy expands this one file; the repo never holds a commit in it.
    assert (REAL_PACKAGE / "BUILD_COMMIT").read_bytes() == b"$Format:%H$\n"


def test_the_stamp_sits_outside_orders_and_gitattributes_marks_only_it():
    stamp = REAL_PACKAGE / "BUILD_COMMIT"
    assert (REAL_PACKAGE / "orders") not in stamp.parents
    lines = [
        line.strip()
        for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    subst = [line for line in lines if "export-subst" in line]
    assert subst == ["gateway/vm/gateway_vm/BUILD_COMMIT export-subst"]
    assert not any("orders" in line for line in lines)
    assert not (REAL_PACKAGE / "orders" / "BUILD_COMMIT").exists()


def test_the_live_orders_tree_hashes_under_the_cap():
    hashed = vm_build._hash_orders(REAL_PACKAGE)
    assert vm_build.MAX_ORDERS_FILES == 64
    assert 1 <= len(hashed) <= 64
    assert "build.py" in hashed and not any("__pycache__" in key for key in hashed)


# ---------------------------------------------------------------- route table


def test_the_route_is_in_the_order_route_set_and_the_two_constants_match():
    assert vm_orders.ROUTE_BUILD == mac_orders.ROUTE_BUILD == BUILD_PATH
    assert vm_orders.ROUTE_BUILD in vm_orders.ORDER_ROUTES
    from gateway_vm.events import ROUTE_VALUES

    assert BUILD_PATH in ROUTE_VALUES


def test_the_mounted_route_is_in_the_order_route_set(stack):
    mounted = {r.path for r in stack.app.routes if getattr(r, "path", "").startswith("/v1/orders")}
    assert BUILD_PATH in mounted
    assert mounted == set(vm_orders.ORDER_ROUTES)


# ---------------------------------------------------------------- gate before answer


def assert_no_build_in(resp) -> None:
    assert COMMIT not in resp.text and "orders_sha256" not in resp.text and "digest" not in resp.text


def test_the_bearer_is_checked_before_anything_is_answered(stack, token):
    assert build_read(stack, token).status_code == 200
    previous = token
    current = relogin(stack, previous)
    for bearer in (None, "wrong-bearer", previous):
        resp = build_read(stack, bearer)
        assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHORIZED"
        assert resp.headers[vm_contract.HDR_CONTRACT] == vm_orders.CONTRACT
        assert_no_build_in(resp)
    assert build_read(stack, current).status_code == 200


@pytest.mark.parametrize("bearer", [None, "never-bound"], ids=["no_bearer", "any_bearer"])
def test_before_any_login_the_answer_is_no_session(stack, bearer):
    resp = build_read(stack, bearer)
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "NO_SESSION")
    assert_no_build_in(resp)


def test_a_revoked_session_answers_no_session(stack, token):
    assert stack.revoke(token).status_code == 200
    for bearer in (token, None):
        resp = build_read(stack, bearer)
        assert (resp.status_code, resp.json()["error"]["code"]) == (503, "NO_SESSION")
        assert_no_build_in(resp)


def test_an_expired_session_answers_session_expired(stack, token):
    stack.clock.advance(15 * 3600)
    resp = build_read(stack, token)
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "SESSION_EXPIRED")
    assert_no_build_in(resp)


def test_the_unstamped_answer_also_waits_for_the_bearer(stack, package):
    (package / "BUILD_COMMIT").write_bytes(b"$Format:%H$\n")
    stack.restart()
    stack.login()
    resp = build_read(stack, "wrong-bearer")
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------- no side effects


@pytest.mark.parametrize(
    "setup",
    [_kill_absent, _kill_blocked, _egress_evidence_aged_out, _budget_exhausted],
    ids=["kill_absent", "kill_blocked", "egress_aged_out", "budget_exhausted"],
)
def test_the_read_needs_no_kill_flag_egress_check_or_budget(stack, token, setup):
    setup(stack)
    requests = list(stack.transport.requests)
    methods = list(stack.metadata.methods)
    kinds = list(stack.budget.kinds)
    resp = build_read(stack, token)
    assert resp.status_code == 200, resp.text
    assert stack.transport.requests == requests
    assert stack.metadata.methods == methods  # no kill read
    assert stack.budget.kinds == kinds  # no charge


def test_twenty_reads_write_nothing_and_leave_the_audit_alone(stack, token, tmp_path):
    entries_before = stack.audit(token)
    state_before = stack.state(token)
    files = tree_snapshot(tmp_path)
    for _ in range(20):
        assert build_read(stack, token).status_code == 200
    assert tree_snapshot(tmp_path) == files
    assert stack.audit(token) == entries_before
    assert stack.state(token) == state_before


def test_events_hold_one_ok_line_per_read_and_no_hashes(stack, token):
    before = len(request_events(stack))
    resp = build_read(stack, token)
    events = request_events(stack)
    assert len(events) == before + 1
    assert (events[-1]["route"], events[-1]["result"], events[-1]["http"]) == (BUILD_PATH, "ok", 200)
    body = resp.json()
    log = stack.sink.getvalue()
    assert body["digest"] not in log and body["commit"] not in log


# ---------------------------------------------------------------- Mac client: strict parse


def good_body(**overrides) -> dict:
    body = {
        "contract": MAC_ORDERS_CONTRACT,
        "commit": COMMIT,
        "orders_sha256": dict(GOLDEN_MAP),
        "digest": GOLDEN_DIGEST,
    }
    body.update(overrides)
    return body


def with_digest(body: dict) -> dict:
    body["digest"] = mac_orders.build_digest(body["commit"], body["orders_sha256"])
    return body


def test_a_good_reply_returns_the_build_after_one_get():
    client, seen = canned_client(orders_reply(good_body()))
    got = client.current_build()
    assert got == VmBuild(COMMIT, dict(sorted(GOLDEN_MAP.items())), GOLDEN_DIGEST)
    assert [(r.method, r.url.path, r.url.query) for r in seen] == [("GET", BUILD_PATH, b"")]
    assert seen[0].headers[vm_contract.HDR_CONTRACT] == MAC_ORDERS_CONTRACT
    assert seen[0].content == b""


def test_a_digest_that_does_not_recompute_is_refused():
    client, seen = canned_client(orders_reply(good_body(digest="d" * 64)))
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == "digest_mismatch"
    assert len(seen) == 1


def test_a_changed_map_or_commit_under_an_unchanged_digest_is_refused():
    changed_map = good_body(orders_sha256={**GOLDEN_MAP, "a.py": "0" * 64})
    other_commit = good_body(commit="a" * 40)
    for body in (changed_map, other_commit):
        client, _ = canned_client(orders_reply(body))
        with pytest.raises(OrdersBuildInvalid) as caught:
            client.current_build()
        assert caught.value.reason == "digest_mismatch"


def test_the_vm_answer_over_the_real_route_passes_the_clients_recompute(stack, token):
    client = OrdersClient(RelayClient(http=stack.client, token_store=mac_store(token)))
    got = client.current_build()
    answered = build_read(stack, token).json()
    assert (got.commit, dict(got.orders_sha256), got.digest) == (
        answered["commit"],
        answered["orders_sha256"],
        answered["digest"],
    )
    assert got.commit == COMMIT


BAD_SHAPES = {
    "extra_key": good_body(note="x"),
    "missing_commit": {k: v for k, v in good_body().items() if k != "commit"},
    "missing_map": {k: v for k, v in good_body().items() if k != "orders_sha256"},
    "missing_digest": {k: v for k, v in good_body().items() if k != "digest"},
    "commit_len_39": with_digest(good_body(commit="a" * 39)),
    "commit_uppercase": with_digest(good_body(commit="A" * 40)),
    "commit_placeholder": with_digest(good_body(commit="$Format:%H$")),
    "commit_int": good_body(commit=7),
    "map_is_list": good_body(orders_sha256=[["a.py", "a" * 64]]),
    "map_value_uppercase": with_digest(good_body(orders_sha256={"a.py": "A" * 64})),
    "map_value_short": with_digest(good_body(orders_sha256={"a.py": "a" * 63})),
    "map_value_int": good_body(orders_sha256={"a.py": 1}),
    "digest_uppercase": good_body(digest=GOLDEN_DIGEST.upper()),
    "digest_short": good_body(digest="a" * 63),
    "digest_null": good_body(digest=None),
}


@pytest.mark.parametrize("body", BAD_SHAPES.values(), ids=BAD_SHAPES.keys())
def test_any_other_shape_is_refused(body):
    client, seen = canned_client(orders_reply(body))
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == "bad_field"
    assert len(seen) == 1


def raw_json(text: str):
    return orders_reply(text.encode("utf-8"))


DUPLICATES = {
    "top_commit": '{"contract":"growin-orders/1","commit":"%s","commit":"%s","orders_sha256":{"a.py":"%s"},"digest":"%s"}'
    % (COMMIT, COMMIT, "a" * 64, "d" * 64),
    "top_digest": '{"contract":"growin-orders/1","commit":"%s","orders_sha256":{"a.py":"%s"},"digest":"%s","digest":"%s"}'
    % (COMMIT, "a" * 64, "d" * 64, "d" * 64),
    "contract": '{"contract":"growin-orders/1","contract":"growin-orders/1","commit":"%s","orders_sha256":{"a.py":"%s"},"digest":"%s"}'
    % (COMMIT, "a" * 64, "d" * 64),
    "map_path": '{"contract":"growin-orders/1","commit":"%s","orders_sha256":{"a.py":"%s","a.py":"%s"},"digest":"%s"}'
    % (COMMIT, "a" * 64, "b" * 64, "d" * 64),
}


@pytest.mark.parametrize("text", DUPLICATES.values(), ids=DUPLICATES.keys())
def test_a_duplicate_key_anywhere_is_refused_even_when_both_values_agree(text):
    client, seen = canned_client(raw_json(text))
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == "duplicate_key"
    assert len(seen) == 1


def test_the_same_body_without_the_duplicate_parses_to_a_digest_check():
    # Control: the fixtures above fail on the duplicate, not on the made-up digest.
    text = '{"contract":"growin-orders/1","commit":"%s","orders_sha256":{"a.py":"%s"},"digest":"%s"}' % (
        COMMIT,
        "a" * 64,
        "d" * 64,
    )
    client, _ = canned_client(raw_json(text))
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == "digest_mismatch"


def build_with_files(files: dict[str, str]) -> dict:
    return with_digest(good_body(orders_sha256=files))


def test_the_map_size_is_one_to_sixty_four():
    sixty_four = {f"f{i:02d}.py": "a" * 64 for i in range(64)}
    client, _ = canned_client(orders_reply(build_with_files(sixty_four)))
    assert len(client.current_build().orders_sha256) == 64
    one = {"a.py": "a" * 64}
    client, _ = canned_client(orders_reply(build_with_files(one)))
    assert len(client.current_build().orders_sha256) == 1
    for files in ({}, {f"f{i:02d}.py": "a" * 64 for i in range(65)}):
        client, _ = canned_client(orders_reply(build_with_files(files)))
        with pytest.raises(OrdersBuildInvalid) as caught:
            client.current_build()
        assert caught.value.reason == "bad_field"


PATHS_REFUSED = {
    "parent": "../limits.py",
    "absolute": "/abs",
    "length_97": "a" * 97,
    "inner_parent": "data/../limits.py",
    "uppercase": "Build.py",
    "leading_dot": ".hidden",
    "double_slash": "data//x.json",
    "backslash": "data\\x.json",
    "empty": "",
    "space": "a b.py",
}
PATHS_ACCEPTED = ["a.py", "data/x.json", "_private.py", "a" * 96, "data/deep/x-y_z.json", "0.py"]


@pytest.mark.parametrize("path", PATHS_REFUSED.values(), ids=PATHS_REFUSED.keys())
def test_a_path_with_a_bad_shape_is_refused(path):
    client, _ = canned_client(orders_reply(build_with_files({path: "a" * 64})))
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == "bad_field"


@pytest.mark.parametrize("path", PATHS_ACCEPTED)
def test_a_well_formed_path_is_accepted(path):
    client, _ = canned_client(orders_reply(build_with_files({path: "a" * 64})))
    assert path in client.current_build().orders_sha256


@pytest.mark.parametrize(
    "reply",
    [
        orders_reply({"commit": COMMIT, "orders_sha256": GOLDEN_MAP, "digest": GOLDEN_DIGEST}),
        orders_reply(good_body(contract="growin-relay/1")),
    ],
    ids=["no_contract", "other_contract"],
)
def test_a_missing_or_other_body_contract_is_a_contract_mismatch(reply):
    client, _ = canned_client(reply)
    with pytest.raises(RelayContractMismatch):
        client.current_build()


@pytest.mark.parametrize(
    "reply, reason",
    [
        (orders_reply(b"not json"), "bad_json"),
        (orders_reply([COMMIT]), "bad_json"),
        (orders_reply(b'{"contract":"growin-orders/1","commit":NaN}'), "bad_json"),
        (orders_reply(good_body(), ctype="text/plain"), "content_type"),
    ],
    ids=["not_json", "array", "nan_constant", "content_type"],
)
def test_non_json_arrays_nan_and_wrong_content_types_are_refused(reply, reason):
    client, _ = canned_client(reply)
    with pytest.raises(OrdersBuildInvalid) as caught:
        client.current_build()
    assert caught.value.reason == reason


# ---------------------------------------------------------------- Mac client: typed errors, no fallback


def test_a_404_is_route_unavailable_with_no_audit_or_health_fallback():
    client, seen = canned_client(orders_error("NOT_FOUND", 404))
    with pytest.raises(OrdersBuildRouteUnavailable) as caught:
        client.current_build()
    assert isinstance(caught.value, NotFound)
    assert (caught.value.reason, caught.value.http_status) == ("route_unavailable", 404)
    assert [(r.method, r.url.path) for r in seen] == [("GET", BUILD_PATH)]  # no audit, no health, no retry


def test_a_404_from_an_older_vm_over_the_real_app_is_the_same(stack, token):
    stack.app.router.routes[:] = [r for r in stack.app.router.routes if getattr(r, "path", "") != BUILD_PATH]
    recorder = Recorder(stack.client)
    client = OrdersClient(RelayClient(http=recorder, token_store=mac_store(token)))
    with pytest.raises(OrdersBuildRouteUnavailable):
        client.current_build()
    assert recorder.calls == [("GET", BUILD_PATH)]
    assert all(e["route"] not in {"/v1/orders/audit", "/v1/health"} for e in request_events(stack))


def test_build_unstamped_is_its_own_class_and_other_unavailable_codes_are_not():
    client, seen = canned_client(orders_error("ORDERS_UNAVAILABLE", 503, "build_unstamped"))
    with pytest.raises(OrdersBuildUnstamped) as caught:
        client.current_build()
    assert isinstance(caught.value, OrdersUnavailable)
    assert (caught.value.reason, caught.value.http_status) == ("build_unstamped", 503)
    assert len(seen) == 1
    other, _ = canned_client(orders_error("ORDERS_UNAVAILABLE", 503, "config_invalid"))
    with pytest.raises(OrdersUnavailable) as plain:
        other.current_build()
    assert not isinstance(plain.value, OrdersBuildUnstamped)


def test_an_unstamped_real_vm_gives_build_unstamped_end_to_end(unstamped_stack):
    token = unstamped_stack.login()
    client = OrdersClient(RelayClient(http=unstamped_stack.client, token_store=mac_store(token)))
    with pytest.raises(OrdersBuildUnstamped):
        client.current_build()


@pytest.mark.parametrize(
    "reply, expected",
    [
        (orders_error("UNAUTHORIZED", 401), Unauthorized),
        (orders_error("METHOD_NOT_ALLOWED", 405), MethodNotAllowed),
    ],
    ids=["401", "405"],
)
def test_every_other_error_passes_through_unchanged(reply, expected):
    client, seen = canned_client(reply)
    with pytest.raises(expected) as caught:
        client.current_build()
    assert not isinstance(caught.value, (OrdersBuildRouteUnavailable, OrdersBuildUnstamped, OrdersBuildInvalid))
    assert [(r.method, r.url.path) for r in seen] == [("GET", BUILD_PATH)]


# ---------------------------------------------------------------- CLI


def run_build_cli(client_factory, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    code = cli_mod.main(["orders-build", *args], client_factory=client_factory, out=out)
    return code, out.getvalue()


def test_cli_prints_commit_and_digest_and_exits_zero():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: orders_reply(good_body())),
        base_url="http://127.0.0.1:8443",
    )
    code, text = run_build_cli(lambda: RelayClient(http=client, token_store=mac_store()))
    assert code == 0
    assert text == f"commit {COMMIT}\ndigest {GOLDEN_DIGEST}\n"
    assert MAC_TOKEN not in text


def test_cli_files_adds_one_line_per_file_in_sorted_order():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: orders_reply(good_body())),
        base_url="http://127.0.0.1:8443",
    )
    code, text = run_build_cli(lambda: RelayClient(http=client, token_store=mac_store()), "--files")
    assert code == 0
    assert text.splitlines() == [
        f"commit {COMMIT}",
        f"digest {GOLDEN_DIGEST}",
        f"file a.py {'a' * 64}",
        f"file b.py {'b' * 64}",
        f"file data/x.json {'c' * 64}",
    ]


@pytest.mark.parametrize(
    "reply, expected",
    [
        (orders_error("NOT_FOUND", 404), "NOT_FOUND (request %s) reason route_unavailable" % "0123456789abcdef"),
        (
            orders_error("ORDERS_UNAVAILABLE", 503, "build_unstamped"),
            "ORDERS_UNAVAILABLE (request %s) reason build_unstamped" % "0123456789abcdef",
        ),
    ],
    ids=["404", "unstamped"],
)
def test_cli_refusals_exit_one_with_the_code_and_reason_only(reply, expected):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: reply), base_url="http://127.0.0.1:8443")
    code, text = run_build_cli(lambda: RelayClient(http=client, token_store=mac_store()))
    assert code == cli_mod.EXIT_RELAY == 1
    assert text == expected + "\n"
    assert MAC_TOKEN not in text and "commit" not in text.replace("commit ", "") and COMMIT not in text


def test_cli_over_the_real_route_prints_the_vm_commit(stack, token):
    code, text = run_build_cli(lambda: RelayClient(http=Recorder(stack.client), token_store=mac_store(token)))
    assert code == 0
    assert text.splitlines()[0] == f"commit {COMMIT}"
    assert token not in text
