## 2024-05-23 - Custom Selection Controls in SwiftUI
**Learning:** Custom selection controls (like segmented controls built with `HStack` + `Button`) must explicitly add `.accessibilityAddTraits([.isSelected])` when the item is active. Without this, VoiceOver users have no way to know which option is currently selected.
**Action:** When creating custom pickers, always bind the `.isSelected` trait to the selection state logic.
