## 2024-10-18 - Custom Selection Controls & Accessibility
**Learning:** Custom SwiftUI selection controls (like `AccountPicker`) don't automatically communicate their "selected" state to screen readers, unlike native `Picker`.
**Action:** Always manually apply `.accessibilityAddTraits([.isSelected])` to the active element in custom segmented controls or tab bars.
