with open("tests/backend/test_newsdata_endpoint.py", "r") as f:
    content = f.read()

replacement = """import requests
import os
import pytest
from dotenv import load_dotenv

load_dotenv("backend/.env")

api_key = os.getenv("NEWSDATA_API_KEY")

@pytest.mark.skipif(not api_key, reason="NEWSDATA_API_KEY environment variable not set")
def test_market_endpoint():
    print(f"Testing NewsData.io 'market' endpoint with key: {api_key[:5]}...")"""

content = content.replace("""import requests
import os
from dotenv import load_dotenv

load_dotenv("backend/.env")

api_key = os.getenv("NEWSDATA_API_KEY")

def test_market_endpoint():
    print(f"Testing NewsData.io 'market' endpoint with key: {api_key[:5]}...")""", replacement)

with open("tests/backend/test_newsdata_endpoint.py", "w") as f:
    f.write(content)
