# Fix Alpaca API Keys Loading Issue

## Problem Analysis

The Alpaca API keys are correctly set in `.env` file but the AlpacaClient reports "API keys not set. Running in offline/mock mode."

### Root Cause Identified

**Environment Loading Order Issue:**
1. **Main Server:** `server.py` imports `data_engine.py` which initializes `AlpacaClient`
2. **AlpacaClient:** Reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` from environment at initialization
3. **Dotenv Loading:** Only happens in `trading212_mcp_server.py` (separate MCP process)
4. **Result:** AlpacaClient initializes before `.env` is loaded → API keys appear missing

### Current Architecture
```mermaid
graph TD
    A[server.py] -->|import| B[data_engine.py]
    B -->|initialize| C[AlpacaClient]
    C -->|read env| D[ALPACA_API_KEY]
    D -->|❌ Not loaded yet| E[Mock Mode]

    F[trading212_mcp_server.py] -->|load_dotenv()| G[.env file]
    G -->|✅ Keys available| H[MCP Process]
```

### Evidence
- ✅ `.env` file contains correct `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
- ✅ Trading212 keys work (loaded by MCP server)
- ❌ Alpaca keys missing despite being in `.env`
- ❌ Chart requests fallback to mock mode

## Proposed Solution

### Move Dotenv Loading to Main Server

**Current (Broken):**
```python
# server.py - No dotenv loading
from data_engine import get_alpaca_client  # ❌ Keys not loaded yet

# trading212_mcp_server.py - Only MCP loads dotenv
load_dotenv()  # ✅ But too late for AlpacaClient
```

**Fixed:**
```python
# server.py - Load dotenv immediately
from dotenv import load_dotenv
load_dotenv()  # ✅ Load before any imports

from data_engine import get_alpaca_client  # ✅ Keys now available
```

### Implementation Steps

#### Phase 1: Immediate Fix
1. **Add dotenv import to server.py**
2. **Load .env before any other imports**
3. **Test Alpaca connectivity**

#### Phase 2: Architecture Improvement
1. **Centralize environment loading** in one place
2. **Add validation** that required keys are loaded
3. **Document environment requirements**

#### Phase 3: Testing & Validation
1. **Verify Alpaca API works** with real keys
2. **Test chart endpoints** return real data
3. **Ensure Trading212** still works
4. **Check other API integrations**

## Technical Details

### Required Code Changes

**server.py** (add at top):
```python
# Load environment variables FIRST
from dotenv import load_dotenv
load_dotenv()

# Now safe to import modules that need env vars
from data_engine import get_alpaca_client
```

### Environment Variables Required
```bash
# Alpaca API (for market data)
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Trading212 API (for portfolio data)  
TRADING212_API_KEY=your_key_here
TRADING212_API_SECRET=your_secret_here

# Other APIs
NEWSAPI_KEY=your_key_here
TAVILY_API_KEY=your_key_here
```

### Validation Logic
```python
def validate_environment():
    """Validate that required environment variables are set"""
    required = ['ALPACA_API_KEY', 'ALPACA_SECRET_KEY']
    missing = [key for key in required if not os.getenv(key)]
    
    if missing:
        logger.warning(f"Missing required environment variables: {missing}")
        logger.warning("Some features will run in mock/offline mode")
    
    return len(missing) == 0
```

## Risk Assessment

**Low Risk:**
- ✅ Only adds dotenv loading, no breaking changes
- ✅ Trading212 already works (dotenv loaded in MCP)
- ✅ Backward compatible with existing setup
- ✅ Easy rollback if issues

**Mitigation:**
- Test in development environment first
- Validate all API integrations still work
- Monitor for any import order issues

## Success Criteria

✅ **Alpaca API keys loaded** correctly on server startup
✅ **Chart requests return real data** instead of mock/fallback
✅ **No regression** in Trading212 functionality
✅ **Clear error messages** if keys are missing
✅ **Environment validation** on startup

## Implementation Priority

**High Priority (Fix Now):**
- Alpaca API integration is broken
- Chart functionality unusable
- Affects core market data features

**Medium Priority (Future):**
- Centralize environment loading
- Add comprehensive validation
- Improve error messages

This fix will restore Alpaca market data functionality and ensure all API integrations work correctly with their configured keys.</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/fix-dotenv-loading.md