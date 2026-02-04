# Palette's Design Journal

## 2024-10-24 - Accessibility in Complex Lists
**Learning:** For complex list items like `ConversationCard` that contain multiple text elements and state (like editing/selection), using `.accessibilityElement(children: .combine)` significantly reduces VoiceOver noise.
**Action:** Always consolidate complex list rows into a single accessibility element with a computed label that includes state, title, and key details.
