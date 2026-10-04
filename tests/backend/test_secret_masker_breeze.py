"""Breeze-shaped secrets must never reach a formatted log line (Phase 61, GATE-03).

All values are generated at runtime. Nothing here is a real credential.
"""

import base64
import logging
import secrets

import pytest

from app_logging import SecretMaskingFormatter
from utils.secret_masker import SecretMasker


def fake_session_token() -> str:
    digits = "".join(secrets.choice("0123456789") for _ in range(8))
    return base64.b64encode(f"FAKEUSER:{digits}".encode()).decode()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def capture():
    logger = logging.getLogger(f"test_masker_breeze_{secrets.token_hex(4)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _Capture()
    handler.setFormatter(SecretMaskingFormatter("%(message)s"))
    logger.addHandler(handler)
    yield logger, handler
    logger.removeHandler(handler)


# ---------------------------------------------------------------- tracer


def test_tracer_dict_arg_session_token_masked(capture):
    logger, handler = capture
    token = fake_session_token()
    logger.info("%s", {"session_token": token})
    line = handler.lines[-1]
    assert token not in line
    assert token[-4:] not in line
    assert "session_token" in line


def test_tracer_sessiontoken_camel_case(capture):
    logger, handler = capture
    token = fake_session_token()
    logger.info("%s", {"SessionToken": token})
    line = handler.lines[-1]
    assert token not in line
    assert token[-4:] not in line
    assert "SessionToken" in line


def test_tracer_header_style_key_with_repr(capture):
    logger, handler = capture
    token = fake_session_token()
    logger.info("%r", {"X-SessionToken": token})
    line = handler.lines[-1]
    assert token not in line
    assert token[-4:] not in line
    assert "X-SessionToken" in line


def test_mask_value_reveals_nothing():
    assert SecretMasker.mask_value("x" * 30) == "***MASKED***"
    assert SecretMasker.mask_value("short") == "***MASKED***"
    assert SecretMasker.mask_value(1234) == "***MASKED***"


# ------------------------------------------------------- string patterns


def fake_hex64() -> str:
    import hashlib
    import os

    return hashlib.sha256(os.urandom(16)).hexdigest()


def fake_app_key() -> str:
    return "AK" + secrets.token_hex(4) + "^=#" + secrets.token_hex(4)


@pytest.mark.parametrize(
    "template, marker",
    [
        ("GET /?apisession={v} HTTP/1.1", "apisession="),
        ("?api_session={v}&x=1", "api_session="),
        ("X-Checksum: token {h}", "X-Checksum"),
        ("x-appkey: {v}", "x-appkey"),
        ("X-SessionToken: {v}", "X-SessionToken"),
        ('{{"AppKey":"{v}","SessionToken":"{v2}"}}', "AppKey"),
        ('{{"secret_key": "{v}"}}', "secret_key"),
        ("token {h}", "token"),
        ("{{'AppKey': '{v}', 'x': 1}}", "AppKey"),
        ("Cookie: a={v}; b={v2}", "Cookie"),
    ],
)
def test_mask_string_breeze_shapes(template, marker):
    v = secrets.token_urlsafe(12)
    v2 = secrets.token_urlsafe(12)
    h = fake_hex64()
    text = template.format(v=v, v2=v2, h=h)
    out = SecretMasker.mask_string(text)
    assert v not in out
    assert v2 not in out
    assert h not in out
    assert marker in out
    assert "***MASKED***" in out


def test_mask_string_app_key_with_special_chars():
    key = fake_app_key()
    out = SecretMasker.mask_string(f'{{"AppKey": "{key}"}}')
    assert key not in out
    assert "AppKey" in out


def test_mask_string_query_keeps_other_params():
    out = SecretMasker.mask_string("?api_session=ABC123&x=1")
    assert "ABC123" not in out
    assert "x=1" in out


def test_mask_string_is_idempotent():
    once = SecretMasker.mask_string("X-Checksum: token " + fake_hex64())
    assert SecretMasker.mask_string(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "Fetched 25 bars for RELIND in 120 ms",
        "cookie jar empty",
        "Authorization header: Bearer ***MASKED***",
    ],
)
def test_ordinary_text_unchanged(text):
    assert SecretMasker.mask_string(text) == text


# --------------------------------------------------------- dict key rules


@pytest.mark.parametrize(
    "key",
    [
        "session_token", "api_secret", "apisession", "SessionToken",
        "X-Checksum", "secret_key", "AppKey", "appKey", "x_app_key",
        "sessionKey", "my_credential_id", "refresh-token",
    ],
)
def test_mask_structure_sensitive_keys(key):
    value = secrets.token_urlsafe(18)
    out = SecretMasker.mask_structure({key: value})
    assert out[key] == "***MASKED***"


def test_numeric_counters_under_token_keys_are_left_alone():
    data = {"max_tokens": 1024, "total_tokens": 5}
    assert SecretMasker.mask_structure(data) == data


def test_exact_key_masks_any_type():
    assert SecretMasker.mask_structure({"token": 1234}) == {"token": "***MASKED***"}


def test_tuple_recursion_keeps_tuple_type():
    out = SecretMasker.mask_structure({"a": ("password", "hunter2")})
    assert isinstance(out["a"], tuple)
    out = SecretMasker.mask_structure({"a": ("x", {"password": "hunter2"})})
    assert out["a"][1]["password"] == "***MASKED***"


def test_int_keys_do_not_raise():
    out = SecretMasker.mask_structure({1: "v", "secret": "s"})
    assert out[1] == "v"
    assert out["secret"] == "***MASKED***"


def test_sets_are_processed_and_keep_type():
    token = "token=" + secrets.token_hex(6)
    out = SecretMasker.mask_structure({"s": {token, "plain"}, "f": frozenset({token})})
    assert isinstance(out["s"], set)
    assert isinstance(out["f"], frozenset)
    assert token not in out["s"]
    assert "plain" in out["s"]


# -------------------------------------------- formatter: tracebacks, safety


def test_exception_traceback_is_masked(capture):
    logger, handler = capture
    token = fake_session_token()
    try:
        raise RuntimeError(f"failed session_token={token}")
    except RuntimeError:
        logger.exception("call failed")
    line = handler.lines[-1]
    assert "Traceback" in line
    assert token not in line


def test_stack_info_is_masked(capture):
    logger, handler = capture
    logger.info("probe", stack_info=True, extra={"k": "api_key='sk-leak-in-source'"})
    line = handler.lines[-1]
    assert "Stack (most recent call last)" in line
    assert "sk-leak-in-source" not in line


def test_format_stack_masks_directly():
    fmt = SecretMaskingFormatter("%(message)s")
    out = fmt.formatStack("Stack\n  call(session_token=ABCDEF123456)")
    assert "ABCDEF123456" not in out


def test_masking_failure_yields_suppression_line(capture, monkeypatch):
    logger, handler = capture
    handled = []
    monkeypatch.setattr(handler, "handleError", lambda record: handled.append(record))

    def boom(*_a, **_k):
        raise ValueError("boom with secret-looking-value")

    monkeypatch.setattr(SecretMasker, "mask_structure", boom)
    logger.info("%s", {"password": "hunter2-leak"})
    line = handler.lines[-1]
    assert "log record suppressed" in line
    assert "hunter2-leak" not in line
    assert handled == []
