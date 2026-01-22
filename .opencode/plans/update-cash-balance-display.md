# Update Cash Balance Display in Portfolio and Dashboard

## Current State
Both PortfolioView and DashboardView use a shared `MetricGrid` component that displays 4 metric cards in a 2x2 grid:

1. **Portfolio Value**: Shows `currentValue` (total invested amount)
2. **Total P&L**: Shows `totalPnl`
3. **Return**: Shows `totalPnlPercent`
4. **Cash Balance**: Shows `cashBalance?.total` (all cash, invested + uninvested)

## Requested Changes
1. Change "Cash Balance" to "Available Cash" showing only uninvested cash (`cashBalance?.free`)
2. Add a "Total Amount" section showing complete portfolio value (investments + all cash)
3. Expand grid to accommodate the additional card

## Proposed Solution
Modify the `MetricGrid` struct to:
- Change the Cash Balance card to "Available Cash" using `cashBalance?.free`
- Add a new "Total Amount" card showing `currentValue + cashBalance?.total`
- Change the grid layout from 2x2 to 2x3 (3 columns, 2 rows)

## New Grid Layout
```
Row 1: [Total Amount] [Total P&L] [Return]
Row 2: [Portfolio Value] [Available Cash] [Empty/Spacer]
```

Or alternatively:
```
Row 1: [Total Amount] [Portfolio Value] [Total P&L]
Row 2: [Return] [Available Cash] [Empty/Spacer]
```

## Implementation Details
- **File to modify**: `/Users/sanketmane/Codes/Growin App/Growin/Growin/Views/PortfolioView.swift`
- **Component**: `MetricGrid` struct (lines 299-332)
- **Change grid columns**: `LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 12)`
- **Update Cash Balance card**: Change title to "Available Cash" and value to `summary?.cashBalance?.free ?? 0`
- **Add Total Amount card**: New `MiniMetricCard` with title "Total Amount" and value `String(format: "£%.2f", (summary?.currentValue ?? 0) + (summary?.cashBalance?.total ?? 0))`

## Impact
- Both PortfolioView and DashboardView will automatically get the updated display since they both use the same MetricGrid
- No backend changes needed - the data is already available
- UI will be more informative by clearly separating available cash from total portfolio value

## Testing
After implementation, verify:
- Available Cash shows only free/uninvested cash
- Total Amount shows complete portfolio value (investments + all cash)
- Grid layout displays properly on different screen sizes
- Dashboard view reflects the same changes</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/update-cash-balance-display.md