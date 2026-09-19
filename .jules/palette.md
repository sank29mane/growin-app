## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-05-14 - Empty state UX improvements
**Learning:** Empty states in SwiftUI lack obvious actionable paths by default, causing users to guess how to proceed when starting fresh.
**Action:** Always use the ContentUnavailableView initializers with 'actions' (introduced in iOS 17/macOS 14) to provide helpful Call-to-Actions (CTAs) for empty states.
