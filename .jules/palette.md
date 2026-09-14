## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-05-14 - Interactive Empty States with ContentUnavailableView
**Learning:** Using the newer ContentUnavailableView initializer that accepts an `actions` block provides a significant UX improvement for empty states by allowing direct, in-context Call-To-Actions (CTAs).
**Action:** When designing empty states in SwiftUI, default to using the `actions` block of `ContentUnavailableView` to provide helpful, accessible next steps instead of just static descriptions.
