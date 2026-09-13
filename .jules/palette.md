## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-05-15 - Enhancing Empty States with CTAs
**Learning:** Native `ContentUnavailableView` initializers with `actions` (introduced in iOS 17/macOS 14) provide a great way to add helpful Call-to-Actions (CTAs) to empty states, significantly improving user onboarding and task resumption.
**Action:** Always upgrade empty states to use the initializer that supports `actions` when a logical next step exists for the user.
