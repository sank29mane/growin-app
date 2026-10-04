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
