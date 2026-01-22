# Debug Dashboard "No Data" Issue

## Problem Description
The Dashboard dual account view shows "No Data" in both INVEST and ISA sections, with no holdings displayed below. This suggests that `investData` and `isaData` are nil in the DashboardViewModel.

## Possible Root Causes

### 1. Backend API Response Issues
- `/portfolio/live?account_type=all` not returning expected structure
- `accounts` field missing or empty in response
- Positions not having `account_type` field set

### 2. Frontend Parsing Issues
- `PortfolioSnapshot` model doesn't include `accounts` field directly
- `parseAccountData()` failing to extract account data
- `AccountData` objects not being created

### 3. Data Flow Issues
- API call failing silently
- JSON decoding errors
- Account keys not matching expected values ("invest", "isa")

## Investigation Steps

### Step 1: Verify Backend Response
**Check what `/portfolio/live?account_type=all` actually returns:**
```bash
curl "http://127.0.0.1:8002/portfolio/live?account_type=all"
```

Expected structure:
```json
{
  "summary": {
    "total_positions": 10,
    "current_value": 50000.0,
    "total_pnl": 1000.0,
    "accounts": {
      "invest": { /* AccountSummary */ },
      "isa": { /* AccountSummary */ }
    }
  },
  "positions": [
    { "ticker": "AAPL", "account_type": "invest", ... },
    { "ticker": "TSLA", "account_type": "isa", ... }
  ]
}
```

### Step 2: Check Frontend Decoding
**Add debug logging in DashboardViewModel:**
- Log the raw JSON response
- Log parsed snapshot structure
- Log accounts dictionary contents
- Log position account_type values

### Step 3: Verify Model Compatibility
**Ensure PortfolioSnapshot includes accounts:**
- Currently: `let summary: PortfolioSummary?`
- PortfolioSummary has: `let accounts: [String: AccountSummary]?`
- Confirm this matches backend response

### Step 4: Test Individual Accounts
**Verify single account requests work:**
- Test `/portfolio/live?account_type=invest`
- Test `/portfolio/live?account_type=isa`
- Ensure they return data

## Debug Implementation Plan

### Phase 1: Add Logging
1. **Backend**: Add logging in trading212_mcp_server.py for account_type=all response
2. **Frontend**: Add debug prints in DashboardViewModel parseAccountData()

### Phase 2: Test API Endpoints
1. **Manual testing**: Use curl/browser to test API responses
2. **Verify structure**: Ensure accounts field exists and contains invest/isa keys

### Phase 3: Fix Issues Found
1. **Missing fields**: Add missing fields to models if needed
2. **Wrong keys**: Fix account key mapping if different
3. **Parsing errors**: Fix JSON decoding issues

### Phase 4: Test Fix
1. **Verify data flow**: Ensure investData/isaData are populated
2. **Check UI**: Confirm dashboard sections show data
3. **Test real-time**: Ensure updates work for both accounts

## Expected Findings
Most likely:
- Backend response structure doesn't match frontend expectations
- Account keys are different ("invest" vs "investment")
- Positions missing account_type field
- JSON decoding failing silently

## Files to Check
- `/backend/trading212_mcp_server.py` - Response generation
- `/frontend/ViewModels/DashboardViewModel.swift` - Data parsing
- `/frontend/Models.swift` - Data models
- API endpoint: `/portfolio/live?account_type=all`</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/debug-dashboard-no-data.md