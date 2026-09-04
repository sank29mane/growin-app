
## $(date +%Y-%m-%d) - SwiftUI Accessibility Cleanups
**Learning:** In SwiftUI, `TextEditor` elements without explicit accessibility modifiers lack context for VoiceOver users, and a standard button initialized with a text string (e.g., `Button("Cancel")`) automatically receives the `.isButton` accessibility trait and uses the string as its default accessibility label, making explicit `.accessibilityLabel` or `.accessibilityAddTraits(.isButton)` redundant.
**Action:** Always append explicit `.accessibilityLabel` and `.accessibilityHint` modifiers to `TextEditor` elements, and avoid redundantly applying `.accessibilityLabel` or `.accessibilityAddTraits(.isButton)` to standard text-initialized buttons.
