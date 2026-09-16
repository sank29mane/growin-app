## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-05-14 - Empty State CTAs
**Learning:** The native ContentUnavailableView with actions (introduced in iOS 17/macOS 14) is a great way to add helpful CTAs to empty states without reinventing the wheel.
**Action:** Use ContentUnavailableView initializers with actions for empty states to improve usability.
