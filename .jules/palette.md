## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-06-25 - Upgrading Empty States in SwiftUI
**Learning:** Native `ContentUnavailableView` provides a clean way to add helpful CTAs to empty states (e.g., "Start New Chat" when there are no conversations).
**Action:** Use the `ContentUnavailableView(label:description:actions:)` initializer to add standard button CTAs for better UX on empty screens.
