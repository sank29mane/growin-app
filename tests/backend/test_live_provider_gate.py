"""Tests that can reach a real NewsData.io call stay opt-in.

A local run must never spend provider quota or leave the machine just because a
backend dotenv file exists. The live tests skip unless GROWIN_LIVE_PROVIDER_TESTS=1,
and they read the dotenv file only inside the test, after the opt-in.
"""

import ast
import importlib
import inspect

import pytest

LIVE_OPT_IN_VAR = "GROWIN_LIVE_PROVIDER_TESTS"

LIVE_TESTS = [
    ("test_newsdata_endpoint", "test_market_endpoint"),
    ("test_news_agent", "test_newsdata_integration"),
]


def _reload(module_name):
    module = importlib.import_module(module_name)
    return importlib.reload(module)


@pytest.mark.parametrize("module_name, test_name", LIVE_TESTS)
def test_live_newsdata_test_skips_without_the_opt_in(monkeypatch, module_name, test_name):
    monkeypatch.delenv(LIVE_OPT_IN_VAR, raising=False)
    fn = getattr(_reload(module_name), test_name)
    skips = [m for m in getattr(fn, "pytestmark", []) if m.name == "skipif"]
    assert skips, f"{module_name}::{test_name} has no skipif gate"
    assert any(m.args and m.args[0] is True and LIVE_OPT_IN_VAR in m.kwargs.get("reason", "") for m in skips)


@pytest.mark.parametrize("module_name, test_name", LIVE_TESTS)
def test_live_newsdata_test_runs_with_the_opt_in(monkeypatch, module_name, test_name):
    monkeypatch.setenv(LIVE_OPT_IN_VAR, "1")
    fn = getattr(_reload(module_name), test_name)
    skips = [m for m in fn.pytestmark if m.name == "skipif"]
    assert skips and all(m.args[0] is False for m in skips)
    monkeypatch.delenv(LIVE_OPT_IN_VAR)
    _reload(module_name)


@pytest.mark.parametrize("module_name, test_name", LIVE_TESTS)
def test_live_newsdata_modules_do_not_load_dotenv_at_import(module_name, test_name):
    tree = ast.parse(inspect.getsource(importlib.import_module(module_name)))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names]
            assert "load_dotenv" not in names, f"{module_name} imports load_dotenv at module level"
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []:
            if isinstance(sub, ast.Call):
                target = getattr(sub.func, "id", getattr(sub.func, "attr", ""))
                assert target != "load_dotenv", f"{module_name} calls load_dotenv at module level"


def test_the_live_market_test_keeps_the_key_out_of_the_url_and_error(monkeypatch):
    # Calls the test body directly (the skipif mark does not apply), with no network:
    # requests.get raises the way a failed connection does, echoing its URL.
    import dotenv
    import requests

    canary = "CANARY-newsdata-key-7f3a"
    monkeypatch.setenv("NEWSDATA_API_KEY", canary)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params or {}, headers=headers or {})
        prepared = requests.Request("GET", url, params=params).prepare()
        raise requests.ConnectionError(f"Max retries exceeded with url: {prepared.url}")

    monkeypatch.setattr(requests, "get", fake_get)
    module = importlib.import_module("test_newsdata_endpoint")
    with pytest.raises(requests.ConnectionError) as excinfo:
        module.test_market_endpoint()
    assert canary not in str(excinfo.value)
    assert canary not in seen["url"] and canary not in repr(seen["params"])
    assert seen["headers"].get("X-ACCESS-KEY") == canary
