## 2024-09-06 - Accessibility Context for TextEditor
**Learning:** In SwiftUI, `TextEditor` elements lack default context for VoiceOver users, and standard text buttons have redundant default accessibility traits.
**Action:** Always append explicit `.accessibilityLabel` and `.accessibilityHint` modifiers to `TextEditor` elements, and avoid redundant `.accessibilityLabel` and `.accessibilityAddTraits(.isButton)` on standard initialized string buttons.
