"""AC-1, AC-2 and the loader half of AC-3: the model registry loader.

Fixtures are synthetic. Key values are generated in the test and never written
to a file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from pathlib import Path

import pytest

import model_registry_testkit as kit
from model_registry import (
    ModelRegistry,
    ModelRegistryError,
    ModelRoleMissing,
    load_registry,
    load_registry_file,
)
from model_registry.loader import MAX_REGISTRY_BYTES

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "backend" / "model_registry" / "models.example.json"
STUB_URL = "http://127.0.0.1:9"


def _doc() -> dict:
    return kit.registry_document(STUB_URL)


def _write(tmp_path: Path, document) -> Path:
    path = tmp_path / "models.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --- AC-1 -------------------------------------------------------------------------


def test_example_loads_with_typed_roles_and_providers():
    registry = load_registry_file(EXAMPLE)
    assert isinstance(registry, ModelRegistry)
    assert set(registry.roles) == {
        "coordinator",
        "decision",
        "research",
        "risk_critic",
        "math_codegen",
        "forecaster",
    }
    decision = registry.resolve("decision")
    assert decision.kind == "openai_compatible"
    assert decision.provider_id == "xai"
    assert decision.model == "example-large-model"
    assert decision.max_tokens == 2048
    assert decision.api_key_env == "EXAMPLE_API_KEY"
    forecaster = registry.resolve("forecaster")
    assert forecaster.kind == "hf_local"
    assert forecaster.revision == "main"
    assert forecaster.base_url is None


def test_fingerprint_is_sha256_of_raw_bytes_and_stable():
    raw = EXAMPLE.read_bytes()
    first = load_registry_file(EXAMPLE)
    second = load_registry_file(EXAMPLE)
    assert first.fingerprint == hashlib.sha256(raw).hexdigest()
    assert first.fingerprint == second.fingerprint


def test_fingerprint_follows_the_bytes_not_the_meaning(tmp_path):
    document = _doc()
    compact = tmp_path / "a" / "models.json"
    compact.parent.mkdir()
    compact.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    spaced = tmp_path / "b" / "models.json"
    spaced.parent.mkdir()
    spaced.write_text(json.dumps(document, indent=2), encoding="utf-8")
    assert load_registry_file(compact).fingerprint != load_registry_file(spaced).fingerprint


def test_load_registry_reads_models_json_under_private_dir(tmp_path):
    kit.write_registry(tmp_path, _doc())
    registry = load_registry(tmp_path)
    assert registry.has_role("decision")


def test_example_file_holds_no_key_value_and_no_real_endpoint():
    text = EXAMPLE.read_text(encoding="utf-8")
    document = json.loads(text)
    for provider in document["providers"].values():
        assert "api_key" not in provider
        url = provider.get("base_url") or ""
        assert url == "" or "127.0.0.1" in url or ".invalid" in url
    for needle in ("sk-", "xai-", "Bearer"):
        assert needle not in text


# --- AC-2 -------------------------------------------------------------------------


def _mutate(fn):
    document = copy.deepcopy(_doc())
    fn(document)
    return document


REJECT_CASES = [
    ("schema_version_2", _mutate(lambda d: d.update(schema_version=2)), "SCHEMA_VERSION"),
    ("schema_version_missing", _mutate(lambda d: d.pop("schema_version")), "SCHEMA_VERSION"),
    ("schema_version_bool", _mutate(lambda d: d.update(schema_version=True)), "SCHEMA_VERSION"),
    ("unknown_top_field", _mutate(lambda d: d.update(extra=1)), "UNKNOWN_FIELD"),
    (
        "unknown_provider_field",
        _mutate(lambda d: d["providers"]["lmstudio"].update(verify_tls=False)),
        "UNKNOWN_FIELD",
    ),
    (
        "unknown_role_field",
        _mutate(lambda d: d["roles"]["decision"].update(system_prompt="x")),
        "UNKNOWN_FIELD",
    ),
    ("unknown_role", _mutate(lambda d: d["roles"].update(planner=d["roles"]["decision"])), "UNKNOWN_ROLE"),
    (
        "undefined_provider",
        _mutate(lambda d: d["roles"]["decision"].update(provider="nowhere")),
        "PROVIDER_UNDEFINED",
    ),
    (
        "unknown_kind",
        _mutate(lambda d: d["providers"]["lmstudio"].update(kind="anthropic_native")),
        "UNKNOWN_KIND",
    ),
    (
        "inline_api_key",
        _mutate(lambda d: d["providers"]["xai"].update(api_key="inline-secret-value")),
        "INLINE_API_KEY",
    ),
    (
        "inline_api_key_other_casing",
        _mutate(lambda d: d["providers"]["xai"].update(Token="inline-secret-value")),
        "INLINE_API_KEY",
    ),
    (
        "bad_api_key_env_lowercase",
        _mutate(lambda d: d["providers"]["xai"].update(api_key_env="not_upper")),
        "API_KEY_ENV_INVALID",
    ),
    (
        "bad_api_key_env_value_shaped",
        _mutate(lambda d: d["providers"]["xai"].update(api_key_env="sk-abc123")),
        "API_KEY_ENV_INVALID",
    ),
    (
        "userinfo",
        _mutate(lambda d: d["providers"]["lmstudio"].update(base_url="http://user:pw@127.0.0.1:1234/v1")),
        "BASE_URL_INVALID",
    ),
    (
        "userinfo_empty_password",
        _mutate(lambda d: d["providers"]["lmstudio"].update(base_url="http://user:@127.0.0.1:1234/v1")),
        "BASE_URL_INVALID",
    ),
    (
        "query",
        _mutate(lambda d: d["providers"]["lmstudio"].update(base_url="http://127.0.0.1:1234/v1?x=1")),
        "BASE_URL_INVALID",
    ),
    (
        "fragment",
        _mutate(lambda d: d["providers"]["lmstudio"].update(base_url="http://127.0.0.1:1234/v1#frag")),
        "BASE_URL_INVALID",
    ),
    (
        "bad_scheme",
        _mutate(lambda d: d["providers"]["lmstudio"].update(base_url="ftp://127.0.0.1/v1")),
        "BASE_URL_INVALID",
    ),
    (
        "key_over_plain_http_remote",
        _mutate(lambda d: d["providers"]["xai"].update(base_url="http://example.invalid/v1")),
        "KEY_OVER_HTTP",
    ),
    (
        "hf_local_on_chat_role",
        _mutate(lambda d: d["roles"]["decision"].update(provider="local_checkpoint")),
        "KIND_ROLE_MISMATCH",
    ),
    (
        "openai_compatible_on_forecaster",
        _mutate(lambda d: d["roles"]["forecaster"].update(provider="lmstudio")),
        "KIND_ROLE_MISMATCH",
    ),
    (
        "empty_model",
        _mutate(lambda d: d["roles"]["decision"].update(model="")),
        "FIELD_INVALID",
    ),
    (
        "openai_compatible_without_url",
        _mutate(lambda d: d["providers"]["lmstudio"].pop("base_url")),
        "FIELD_INVALID",
    ),
]


@pytest.mark.parametrize("name,document,code", REJECT_CASES, ids=[c[0] for c in REJECT_CASES])
def test_loader_rejects_with_code_and_leaks_no_file_value(tmp_path, name, document, code):
    path = _write(tmp_path, document)
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(path)
    err = caught.value
    assert err.code == code
    assert isinstance(err.field, str)
    text = str(err) + repr(err) + repr(err.args)
    for planted in ("inline-secret-value", "user:pw", "sk-abc123", "example.invalid"):
        assert planted not in text
    assert err.__cause__ is None


def test_localhost_names_are_loopback_for_key_over_http(tmp_path):
    document = _doc()
    document["providers"]["xai"]["base_url"] = "http://localhost:8080/v1"
    load_registry_file(_write(tmp_path, document))
    document["providers"]["xai"]["base_url"] = "http://[::1]:8080/v1"
    load_registry_file(_write(tmp_path, document))


def test_key_over_https_remote_is_accepted(tmp_path):
    document = _doc()
    document["providers"]["xai"]["base_url"] = "https://api.example.invalid/v1"
    registry = load_registry_file(_write(tmp_path, document))
    assert registry.resolve("decision").base_url == "https://api.example.invalid/v1"


def test_missing_file_code(tmp_path):
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(tmp_path / "models.json")
    assert caught.value.code == "FILE_MISSING"


def test_private_dir_none_code():
    with pytest.raises(ModelRegistryError) as caught:
        load_registry(None)
    assert caught.value.code == "PRIVATE_DIR_MISSING"


def test_symlink_refused(tmp_path):
    (tmp_path / "real").mkdir()
    real = _write(tmp_path / "real", _doc())
    link = tmp_path / "models.json"
    os.symlink(real, link)
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(link)
    assert caught.value.code == "SYMLINK_REFUSED"


def test_non_regular_file_refused(tmp_path):
    directory = tmp_path / "models.json"
    directory.mkdir()
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(directory)
    assert caught.value.code == "NOT_A_REGULAR_FILE"


def test_fifo_refused(tmp_path):
    fifo = tmp_path / "models.json"
    os.mkfifo(fifo)
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(fifo)
    assert caught.value.code == "NOT_A_REGULAR_FILE"


def test_oversize_refused(tmp_path):
    path = tmp_path / "models.json"
    path.write_bytes(b" " * (MAX_REGISTRY_BYTES + 1))
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(path)
    assert caught.value.code == "FILE_TOO_LARGE"


def test_duplicate_key_refused_without_leaking_values(tmp_path):
    secret = secrets.token_hex(8)
    text = (
        '{"schema_version": 1, "schema_version": 1, "providers": {}, "roles": {},'
        f' "note": "{secret}"}}'
    )
    path = tmp_path / "models.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(path)
    assert caught.value.code == "DUPLICATE_KEY"
    assert secret not in str(caught.value)


def test_duplicate_role_key_refused(tmp_path):
    text = json.dumps(_doc())
    text = text.replace('"roles": {', '"roles": {"decision": {"provider": "xai", "model": "m"}, ', 1)
    path = tmp_path / "models.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(path)
    assert caught.value.code == "DUPLICATE_KEY"


@pytest.mark.parametrize(
    "body,code",
    [
        (b"\xff\xfe\x00", "NOT_UTF8"),
        (b"{not json", "INVALID_JSON"),
        (b"[1, 2]", "NOT_AN_OBJECT"),
    ],
)
def test_malformed_bytes_codes(tmp_path, body, code):
    path = tmp_path / "models.json"
    path.write_bytes(body)
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(path)
    assert caught.value.code == code


def test_unknown_field_name_is_sanitised_in_the_error(tmp_path):
    document = _doc()
    hostile = "sk-" + secrets.token_hex(40) + " \n"
    document["providers"]["lmstudio"][hostile] = 1
    with pytest.raises(ModelRegistryError) as caught:
        load_registry_file(_write(tmp_path, document))
    assert caught.value.code == "UNKNOWN_FIELD"
    assert len(caught.value.field) <= 64
    assert "\n" not in str(caught.value) and " " not in caught.value.field


# --- AC-3 (loader half) -------------------------------------------------------------


def test_missing_role_loads_and_fails_closed_for_that_role_only(tmp_path):
    roles = [r for r in ("coordinator", "decision", "risk_critic", "math_codegen")]
    registry = kit.make_registry(tmp_path, STUB_URL, roles=roles)
    assert registry.has_role("decision")
    assert registry.resolve("decision").model == kit.model_id_for("decision")
    with pytest.raises(ModelRoleMissing) as caught:
        registry.resolve("research")
    assert caught.value.code == "ROLE_MISSING"
    assert caught.value.field == "research"
    assert isinstance(caught.value, ModelRegistryError)


def test_unknown_role_name_is_a_missing_role_not_a_crash(tmp_path):
    registry = kit.make_registry(tmp_path, STUB_URL)
    with pytest.raises(ModelRoleMissing) as caught:
        registry.resolve("not_a_role")
    assert caught.value.code == "ROLE_MISSING"


def test_require_raises_for_first_missing_role(tmp_path):
    registry = kit.make_registry(tmp_path, STUB_URL, roles=["coordinator"])
    registry.require("coordinator")
    with pytest.raises(ModelRoleMissing) as caught:
        registry.require("coordinator", "decision")
    assert caught.value.field == "decision"
