# Palette's Journal

## 2024-05-22 - Accessibility in Custom Pickers
**Learning:** Custom selection controls (like `AccountPicker`) built with `HStack` and `Button` often miss the `.isSelected` trait, making it impossible for screen reader users to know the current state.
**Action:** Always check custom segmented controls for `.accessibilityAddTraits(isSelected ? [.isSelected] : [])`.
