## 2024-05-22 - Status Icon Noise in Lists
**Learning:** Lists of steps with status icons (like `ToolExecutionBlock`) create significant noise for VoiceOver users if icons are not hidden.
**Action:** Use `.accessibilityHidden(true)` on decorative status icons and group the row with `.accessibilityElement(children: .combine)` plus a descriptive label (e.g., "Completed step: [Name]").
