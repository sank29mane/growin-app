"""Live NewsData.io market endpoint check.

Hits the real provider, so it runs only on an explicit opt-in:
GROWIN_LIVE_PROVIDER_TESTS=1 (and NEWSDATA_API_KEY set in the environment or in
backend/<dotenv file>). The dotenv file is read inside the test, after the opt-in,
never at import time.
"""

import os

import pytest
import requests

LIVE_OPT_IN_VAR = "GROWIN_LIVE_PROVIDER_TESTS"


@pytest.mark.skipif(
    os.getenv(LIVE_OPT_IN_VAR) != "1",
    reason=f"live NewsData.io call; set {LIVE_OPT_IN_VAR}=1 to run",
)
def test_market_endpoint():
    from dotenv import load_dotenv

    load_dotenv("backend/.env")
    api_key = os.getenv("NEWSDATA_API_KEY")
    if not api_key:
        pytest.skip("NEWSDATA_API_KEY not set")

    url = "https://newsdata.io/api/1/market"
    # The key goes in a header, never the URL: a failed request can echo its URL
    # into the exception text, and pytest prints that.
    params = {"removeduplicate": 0, "excludefield": "ai_summary"}
    headers = {"X-ACCESS-KEY": api_key}
    response = requests.get(url, params=params, headers=headers, timeout=30)
    assert response.status_code == 200, f"market endpoint returned {response.status_code}"
    assert isinstance(response.json().get("results", []), list)


if __name__ == "__main__":
    test_market_endpoint()
