## 2025-02-28 - SwiftUI .buttonStyle(.plain) strips accessibility traits
**Learning:** In SwiftUI, applying `.buttonStyle(.plain)` to a button strips standard VoiceOver attributes, resulting in icon-only buttons having no inherent accessible name or traits.
**Action:** When using `.buttonStyle(.plain)`, manually add `.accessibilityLabel()`, `.accessibilityHint()`, and `.accessibilityAddTraits()` modifiers to ensure the button is recognizable and actionable by assistive technologies.
## 2025-05-18 - SwiftUI `.buttonStyle(.plain)` Accessibility Strip

**Learning:** When applying `.buttonStyle(.plain)` to buttons in SwiftUI (especially complex ones or icon-only ones), it strips out standard VoiceOver traits like `isSelected` or the default accessibility boundaries. The standard text or label might not be read correctly or might be combined improperly by VoiceOver.
**Action:** Always explicitly add `.accessibilityLabel`, `.accessibilityHint`, and `.accessibilityAddTraits` (especially for selected states like `isSelected ? [.isSelected] : []`) when using `.buttonStyle(.plain)`. If the button contains multiple text/image views, consider adding `.accessibilityElement(children: .combine)`.
