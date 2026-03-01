## 2025-02-28 - SwiftUI .buttonStyle(.plain) strips accessibility traits
**Learning:** In SwiftUI, applying `.buttonStyle(.plain)` to a button strips standard VoiceOver attributes, resulting in icon-only buttons having no inherent accessible name or traits.
**Action:** When using `.buttonStyle(.plain)`, manually add `.accessibilityLabel()`, `.accessibilityHint()`, and `.accessibilityAddTraits()` modifiers to ensure the button is recognizable and actionable by assistive technologies.
