# Fix Ticker Normalization Issue (AAPL.L Problem)

## Problem Analysis

### Current Issue
- **AAPL** (Apple Inc. - NASDAQ) is being converted to **AAPL.L** (London Stock Exchange)
- **Alpaca API** cannot find AAPL.L (doesn't exist) → "possibly delisted; no price data found"
- **Chart requests fail** with "No valid bar data could be processed"
- **Root cause**: `normalize_ticker()` incorrectly assumes alphabetic tickers are UK stocks

### Evidence from Code
**Current Logic in `trading212_mcp_server.py`:**
```python
# PROBLEMATIC: Assumes all alphabetic tickers are UK
if not is_uk and '.' not in ticker and ticker.isalpha():
    is_uk = True  # ❌ This is wrong!
```

**Affected Tickers:**
- AAPL → AAPL.L (doesn't exist)
- MSFT → MSFT.L (doesn't exist)
- GOOGL → GOOGL.L (doesn't exist)
- All major US tech stocks incorrectly get .L suffix

### Why This Happens
1. **Trading212** provides "AAPL" (correct US ticker)
2. **normalize_ticker()** sees alphabetic ticker with no dot → assumes UK
3. **Adds .L suffix** → becomes "AAPL.L"
4. **Alpaca API** tries to fetch non-existent "AAPL.L"
5. **Fails** → falls back to mock data or errors

## Proposed Solution

### Phase 1: Immediate Fix - Remove Incorrect Heuristic
**Remove the problematic assumption:**
```python
# REMOVE this incorrect logic:
if not is_uk and '.' not in ticker and ticker.isalpha():
    is_uk = True  # ❌ Remove this
```

**Result:** Only explicitly identified UK tickers get .L suffix

### Phase 2: Improve UK Stock Detection
**Better heuristics for UK stock identification:**

1. **Explicit Trading212 markers:**
   - `_EQ` suffix (no _US prefix)
   - Numeric suffixes (SGLN1 → SGLN.L)
   - Lowercase 'l' typos (SSLNl → SSLN.L)

2. **Known UK exchanges/indicators:**
   - Tickers in FTSE 100/250
   - London-based companies
   - UK-specific naming patterns

3. **Exchange-specific rules:**
   - If from LSE/LONDON → add .L
   - If from NASDAQ/NYSE → no suffix
   - If ambiguous → check against known lists

### Phase 3: US Stock Protection
**Add explicit US stock handling:**

```python
# Known major US stocks that should NOT get .L
US_STOCKS = {
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA', 'NVDA', 'META', 'NFLX',
    'AMD', 'INTC', 'ORCL', 'CRM', 'ADBE', 'NFLX', 'PYPL', 'UBER',
    # Add more major US stocks...
}

def normalize_ticker(ticker: str) -> str:
    # Strip suffixes
    ticker = ticker.replace('_EQ', '').replace('_US', '').replace('_', '')
    
    # If already has exchange suffix, return as-is
    if '.' in ticker:
        return ticker.upper()
    
    # Explicitly prevent .L for known US stocks
    if ticker.upper() in US_STOCKS:
        return ticker.upper()
    
    # Apply UK detection logic only for unknown tickers
    # ... existing UK detection code ...
```

### Phase 4: Fallback & Validation
**Add validation and fallbacks:**

1. **Exchange Validation:** If .L ticker fails, try without .L
2. **Cross-API Verification:** Check both Alpaca and yFinance
3. **User Feedback:** Clear error messages for delisted/invalid tickers
4. **Logging:** Track normalization decisions for debugging

## Implementation Plan

### Phase 1: Quick Fix (1-2 hours)
1. **Remove incorrect heuristic** from normalize_ticker()
2. **Test with AAPL** - should no longer get .L suffix
3. **Verify Alpaca API works** for US stocks

### Phase 2: Robust Solution (2-4 hours)
1. **Create US_STOCKS whitelist** with major tickers
2. **Improve UK detection logic** with better heuristics
3. **Add exchange validation** and fallback logic
4. **Test comprehensive ticker set** (US, UK, international)

### Phase 3: Testing & Validation (1-2 hours)
1. **Test major US stocks:** AAPL, MSFT, GOOGL, TSLA, etc.
2. **Test UK stocks:** LLOY.L, SGLN.L, etc.
3. **Test edge cases:** International stocks, crypto, ETFs
4. **Performance testing:** Ensure normalization doesn't slow down requests

### Phase 4: Monitoring & Maintenance (Ongoing)
1. **Add logging** for normalization decisions
2. **Monitor API failures** and adjust logic as needed
3. **Expand US_STOCKS list** based on usage patterns
4. **Handle new ticker formats** from Trading212 updates

## Technical Details

### Files to Modify
- `/backend/trading212_mcp_server.py` - `normalize_ticker()` function
- `/backend/data_engine.py` - Alpaca client error handling
- `/backend/routes/additional_routes.py` - Chart API fallback logic

### Expected Behavior After Fix
- ✅ **AAPL** → **AAPL** (no .L suffix)
- ✅ **Alpaca API** finds AAPL data successfully
- ✅ **Chart requests** return real market data
- ✅ **UK stocks** still get .L when appropriate
- ✅ **Fallback** to yFinance if Alpaca fails

### Risk Assessment
**Low Risk:**
- ✅ Only affects ticker normalization logic
- ✅ Existing UK stock handling preserved
- ✅ No breaking changes to other functionality
- ✅ Easy rollback if issues

**Testing Required:**
- Major US tech stocks (AAPL, MSFT, GOOGL, AMZN, TSLA)
- UK stocks (LLOY, SGLN, BP)
- International stocks
- Edge cases (crypto, ETFs, options)

## Success Criteria
✅ **AAPL chart requests** return real data instead of "possibly delisted"
✅ **Alpaca API errors** eliminated for major US stocks
✅ **UK stocks** continue working with .L suffix
✅ **No performance regression** in ticker processing
✅ **Clear error messages** for genuinely invalid tickers

This fix will resolve the core issue where US stocks are incorrectly getting London Stock Exchange suffixes, causing API failures and degraded user experience.</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/fix-ticker-normalization.md