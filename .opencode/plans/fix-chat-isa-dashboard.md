# Fix Chat ISA Context and Dashboard Display

## Issue 1: Chat Missing ISA Account Context

### Current Problem
- Chat system only provides context from the active (invest) account
- When users ask about ISA accounts, it gives generic advice without actual portfolio data
- No intelligent detection of which account(s) the user is asking about

### Proposed Solution

#### Query Analysis for Account Detection
**Add account detection logic** in `decision_agent.py` or `market_context.py`:

1. **Keyword Detection**: Scan user query for ISA/invest account mentions
   - "ISA", "isa", "tax-free", "investment account" → ISA context
   - "invest", "investment", "brokerage" → Invest context
   - "both", "all", "portfolio" → Both accounts

2. **Portfolio Data Selection**: 
   - Single account: Fetch data for that account
   - Both accounts: Fetch `account_type=all` data
   - Default: Use active account

#### Configurable Data Granularity
**Add depth parameter** to portfolio context:
- **Summary**: Total values, P&L, top holdings (default for simple queries)
- **Detailed**: Full position list with individual metrics (for specific analysis)
- **Auto**: Determine depth based on query complexity

#### Implementation Changes
1. **Modify `market_context.py`**: Add account detection and configurable depth
2. **Update `decision_agent.py`**: Pass account filter and depth to context gathering
3. **Enhance prompt building**: Include account-specific context in AI prompts

## Issue 2: Dashboard Not Showing Graphs and Holdings

### Current Problem
- Account sections show "No Data" or empty content
- Allocation graphs not rendering
- Holdings lists not displaying under graphs

### Root Cause Analysis
**Possible issues:**
1. **Chart Data**: `allocationData` not being calculated or passed correctly
2. **UI Layout**: Charts/holdings not positioned properly in `AccountSectionView`
3. **Data Flow**: `investData`/`isaData` not populated despite positions existing

### Proposed Solution

#### Fix Chart Display
**Update `AccountSectionView`** to properly display:
1. **Allocation Chart**: Use existing `allocationData` from `AccountData`
2. **Position List**: Show holdings underneath chart like portfolio view
3. **Layout**: Stack chart + holdings vertically per account section

#### Data Validation
**Add debug checks** in `AccountSectionView`:
- Verify `allocationData` has items
- Check positions array is not empty
- Log data availability for troubleshooting

#### UI Improvements
1. **Chart Sizing**: Ensure proper height for allocation charts
2. **Position Display**: Use same `PositionDeepCard` or simplified version
3. **Empty States**: Better handling when account has no data

## Implementation Plan

### Phase 1: Chat Context Fix
1. **Add account detection** in query preprocessing
2. **Modify portfolio fetching** to support account filtering
3. **Update prompt building** with account-specific context
4. **Test with ISA queries** to verify proper context inclusion

### Phase 2: Dashboard Display Fix
1. **Debug data flow** - verify `AccountData` objects are created
2. **Fix chart rendering** - ensure allocation data displays
3. **Add holdings display** - show positions under charts
4. **Test visual layout** - ensure proper spacing and alignment

### Phase 3: Integration Testing
1. **End-to-end testing** - verify both fixes work together
2. **User query testing** - test various account mention scenarios
3. **UI responsiveness** - test dashboard on different screen sizes

## Files to Modify
- `backend/decision_agent.py` - Query analysis and context building
- `backend/market_context.py` - Account filtering and data depth
- `frontend/Views/DashboardView.swift` - AccountSectionView layout and display
- `frontend/ViewModels/DashboardViewModel.swift` - Data validation

## Expected Outcomes
- **Chat**: Provides relevant account context based on user queries
- **Dashboard**: Shows allocation graphs and holdings for both accounts
- **User Experience**: Seamless account-specific interactions across chat and dashboard</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/fix-chat-isa-dashboard.md