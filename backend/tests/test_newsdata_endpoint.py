
import requests
import os
from dotenv import load_dotenv

load_dotenv("backend/.env")

api_key = os.getenv("NEWSDATA_API_KEY")

def test_market_endpoint():
    print(f"Testing NewsData.io 'market' endpoint with key: {api_key[:5]}...")
    
    # User suggested URL structure
    url = "https://newsdata.io/api/1/market"
    params = {
        "apikey": api_key,
        "removeduplicate": 0,
        "excludefield": "ai_summary"
    }
    
    try:
        print(f"GET {url}")
        response = requests.get(url, params=params)
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            results = data.get('results', [])
            print(f"✅ Success! Found {len(results)} items.")
            if results:
                print(f"Sample: {results[0].get('title', 'No Title')}")
        else:
            print(f"❌ Failed: {response.text}")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_market_endpoint()
