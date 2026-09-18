## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-05-14 - Empty States Should Be Actionable
**Learning:** By default, SwiftUI's `ContentUnavailableView` is informative but passive, requiring the user to locate a separate button (like a '+' in a toolbar) to take the obvious next action.
**Action:** When implementing or modifying an empty state (`ContentUnavailableView`), always use the initializer variant that includes the `actions:` closure to provide an immediate, prominent Call-to-Action (CTA) button directly within the empty view (e.g., `.buttonStyle(.borderedProminent)`).
