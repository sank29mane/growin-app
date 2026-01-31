import sys
import importlib.util
from unittest.mock import MagicMock
import pytest

# Mock heavy dependencies immediately upon loading conftest
# Only mock if they are not installed
modules_to_mock = [
    "mcp", "mcp.server", "mcp.server.stdio", "mcp.types",
    "mcp.client", "mcp.client.stdio", "mcp.client.sse",
    "chromadb", "chromadb.config",
    "granite_tsfm",
    "langchain", "langchain_core", "langchain_openai",
    "langchain_anthropic", "langchain_google_genai", "langchain_ollama",
    "yfinance", "pandas", "numpy", "scikit-learn", "xgboost", "prophet",
    "torch", "transformers", "mlx", "mlx_lm", "duckdb",
    "rapidfuzz", "newsapi", "tavily", "vaderSentiment", "psutil", "alpaca_trade_api"
]

for module in modules_to_mock:
    # Check if module is already imported
    if module in sys.modules:
        continue

    # Check if module is installable/available
    try:
        # Handle submodules like mcp.server by checking base package first
        base_module = module.split('.')[0]
        if importlib.util.find_spec(base_module) is None:
            sys.modules[module] = MagicMock()
    except (ImportError, AttributeError, ValueError):
        # If any error during check, assume missing and mock
        sys.modules[module] = MagicMock()
