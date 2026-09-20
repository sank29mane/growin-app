## 2024-05-14 - Accessibility improvements for TextEditor and Button
**Learning:** TextEditor elements lack default context for VoiceOver users, and standard buttons initialized with text strings automatically receive the .isButton trait and label.
**Action:** Always append explicit .accessibilityLabel and .accessibilityHint to TextEditor, and avoid redundantly applying them to standard text-initialized buttons.
## 2024-10-18 - Actionable Empty States
**Learning:** Empty states in conversational lists (`ContentUnavailableView`) initially lacked actionable calls-to-action (CTAs), leading to a confusing UX where users didn't know how to initiate their first action.
**Action:** When designing or updating empty states, utilize `ContentUnavailableView` initializers that support `actions:` (iOS 17+) to embed explicit CTAs (e.g., "New Conversation" button) that change the state (e.g., dismissing the view) and guide the user intuitively.
