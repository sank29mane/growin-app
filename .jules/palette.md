## 2024-09-02 - TextEditor Accessibility and Redundant Button Traits
**Learning:** In SwiftUI, `TextEditor` elements without explicit accessibility modifiers lack context for VoiceOver users, and applying `.accessibilityLabel` or `.accessibilityAddTraits(.isButton)` to standard text-initialized SwiftUI Buttons is a redundant anti-pattern.
**Action:** Always append explicit `.accessibilityLabel` and `.accessibilityHint` modifiers to `TextEditor` elements, and avoid adding redundant accessibility traits/labels to standard SwiftUI Buttons initialized with text.
