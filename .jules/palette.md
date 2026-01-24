## 2026-01-23 - Accessibility in Custom Pickers
**Learning:** Custom selection controls (like `AccountPicker`) using `ForEach` + `Button` often miss state communication to screen readers. Visual indicators (colors/borders) are insufficient.
**Action:** Always add `.accessibilityAddTraits(.isSelected)` to the active element in custom pickers to ensure blind users know the current state.
