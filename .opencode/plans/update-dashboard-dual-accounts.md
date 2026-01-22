# Update Dashboard for Dual Account Display

## Current State
DashboardView currently shows data for a single account (active account) with:
- Overall portfolio metrics
- Allocation pie chart
- Performance metrics
- List of positions (top 10)

## Requested Changes
Show information from both INVEST and ISA accounts simultaneously:
- Left section: INVEST account holdings and metrics
- Right section: ISA account holdings and metrics
- Real-time updates for both accounts
- Maintain overall summary at top

## Backend Support
The `/portfolio/live?account_type=all` endpoint returns:
```json
{
  "summary": { /* combined totals */ },
  "accounts": {
    "invest": { /* invest account summary */ },
    "isa": { /* isa account summary */ }
  },
  "positions": [ /* all positions with accountType field */ ]
}
```

## Implementation Plan

### 1. Update DashboardViewModel
- Modify `fetchPortfolioData()` to use `account_type=all`
- Add properties for invest and ISA account data:
  ```swift
  @Published var investData: AccountData?
  @Published var isaData: AccountData?
  ```
- Parse response to separate data by account type

### 2. Create AccountData Model
- New struct to hold account-specific data:
  ```swift
  struct AccountData {
      let summary: PortfolioSummary
      let positions: [Position]
      let allocationData: [AllocationItem]
  }
  ```

### 3. Update DashboardView UI Layout
Current structure:
```
[Header]
[MetricGrid - overall]
[Charts Row - allocation + performance]
[Positions List - all positions]
```

New structure:
```
[Header]
[MetricGrid - overall combined]
[HStack - Account Sections]
  [VStack - INVEST Account]
    [Mini MetricGrid]
    [Allocation Chart]
    [Positions List]
  [VStack - ISA Account]
    [Mini MetricGrid]
    [Allocation Chart]
    [Positions List]
```

### 4. Real-time Updates
- Keep existing 30-second timer
- Ensure both account sections update simultaneously
- Use existing refresh mechanism

### 5. UI Components Needed
- **AccountSectionView**: Reusable component for each account
- **MiniMetricGrid**: Smaller version of MetricGrid for account sections
- Filter positions by `position.accountType`

## Technical Details

### Data Parsing
```swift
// Parse accounts from response
if let accounts = snapshot.accounts {
    investData = parseAccountData(accounts["invest"])
    isaData = parseAccountData(accounts["isa"])
}
```

### Position Filtering
```swift
let investPositions = snapshot.positions?.filter { $0.accountType == "invest" } ?? []
let isaPositions = snapshot.positions?.filter { $0.accountType == "isa" } ?? []
```

### UI Layout Considerations
- Use `HStack` with equal width for account sections
- Ensure proper spacing and visual hierarchy
- Keep consistent styling with existing design
- Handle cases where one account has no data

## Benefits
- **Complete portfolio view**: See both accounts simultaneously
- **Account comparison**: Easy comparison of holdings and performance
- **Real-time sync**: Both accounts update together
- **Better organization**: Clear separation by account type

## Files to Modify
1. `/Views/DashboardView.swift` - Main UI changes
2. `/ViewModels/DashboardViewModel.swift` - Data fetching and parsing
3. Potentially add new model structs in `Models.swift`

## Testing
Verify:
- Both accounts display correct data
- Real-time updates work for both sections
- Position filtering works correctly
- UI layout is responsive and visually balanced
- Error handling when one account fails to load</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/update-dashboard-dual-accounts.md