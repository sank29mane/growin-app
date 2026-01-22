# Fix Currency Normalization for Portfolio Display

## Problem Analysis
The user reports that ISA account holdings show "outrageously high amounts" - prices appear to be displayed as pence instead of pounds (e.g., £100 displayed as £10,000).

## Root Cause
The backend currency normalization is **adding** normalized price fields (`currentPriceGBP`, `averagePriceGBP`) but **not modifying** the original `currentPrice` and `averagePrice` fields that the frontend reads.

### Current Behavior:
1. Backend fetches position data with prices in pence (GBX)
2. `normalize_position()` adds `currentPriceGBP` (normalized) but leaves `currentPrice` unchanged
3. Frontend reads `currentPrice` (still in pence) and displays it as pounds
4. Result: Pence values displayed as pounds = 100x too high

### Evidence:
- `currency_normalizer.py` normalize_position() adds new fields but preserves raw prices
- Frontend `Position` model only has `currentPrice` (not `currentPriceGBP`)
- Backend uses `currentPriceGBP` for calculations but frontend sees raw `currentPrice`

## Proposed Solution

### Modify `normalize_position()` in `currency_normalizer.py`
**Change the function to modify prices in-place:**

```python
# BEFORE: Adds new field
position['currentPriceGBP'] = normalized_price

# AFTER: Modifies original field
position['currentPrice'] = normalized_price
```

### Why This Approach
- Minimal code changes required
- Maintains backward compatibility
- Frontend automatically gets normalized prices
- No changes needed to frontend models or UI code

### Alternative Approaches Considered
1. **Add currentPriceGBP to frontend models**: Requires Swift model changes and UI updates
2. **Use display fields**: Frontend would need logic to choose between raw/normalized prices
3. **Backend API changes**: More complex, affects all consumers

## Implementation Steps

1. **Modify `currency_normalizer.py`**:
   - Update `normalize_position()` to replace `currentPrice` with normalized value
   - Update `averagePrice` similarly
   - Keep display fields for future use if needed

2. **Test the fix**:
   - Verify ISA accounts show correct prices
   - Check INVEST accounts still work
   - Confirm calculations use correct normalized values

3. **Monitor for regressions**:
   - Ensure portfolio totals still calculate correctly
   - Check that P&L calculations use proper normalized prices

## Files to Modify
- `/Users/sanketmane/Codes/Growin App/backend/currency_normalizer.py` - `normalize_position()` function

## Risk Assessment
- **Low risk**: Only affects price display, not underlying data
- **Isolated change**: Currency normalization is contained in one function
- **Easy rollback**: Can revert the single function change

## Expected Outcome
- ISA account holdings display correct prices (pounds, not pence)
- INVEST accounts continue working normally
- All portfolio calculations remain accurate
- No frontend changes required</content>
<parameter name="filePath">/Users/sanketmane/Codes/Growin App/.opencode/plans/fix-currency-normalization.md